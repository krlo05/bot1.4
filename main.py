import sqlite3
import datetime
import os
import json
from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv
from telegram import Update, Bot, ChatMember
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes
import logging
import asyncio
import threading
from functools import wraps

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cargar variables desde .env
load_dotenv()

# 🔐 Token y constantes
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("⚠️ BOT_TOKEN no está definido como variable de entorno")

DB_NAME = 'members.db'
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5286685895"))
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_URL = os.getenv("https://bot1-4-yzqg.onrender.com", "")  # URL de tu servicio en Render

# 🌐 Crear aplicación Flask
app = Flask(__name__)

# Variables globales
telegram_app = None
bot_status = {
    "running": False,
    "last_check": None,
    "members_count": 0,
    "admin_notified": False,
    "errors": [],
    "webhook_set": False
}

# 🧱 Inicializar DB
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS members (
            user_id INTEGER,
            chat_id INTEGER,
            join_date TEXT,
            PRIMARY KEY (user_id, chat_id)
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("✅ Base de datos inicializada")

# 📊 Obtener estadísticas de la DB
def get_stats():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM members')
        total_members = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT chat_id, COUNT(*) as count 
            FROM members 
            GROUP BY chat_id
        ''')
        groups = cursor.fetchall()
        conn.close()
        
        return {
            "total_members": total_members,
            "groups": [{"chat_id": chat_id, "members": count} for chat_id, count in groups]
        }
    except Exception as e:
        logger.error(f"Error obteniendo estadísticas: {e}")
        return {"total_members": 0, "groups": []}

# 🔄 Función para ejecutar código async en thread separado
def run_async(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(func(*args, **kwargs))
            finally:
                loop.close()
        
        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
    return wrapper

# 📥 Manejo de usuarios que se unen
async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        member_update = update.chat_member
        old_status = member_update.old_chat_member.status if member_update.old_chat_member else "unknown"
        new_status = member_update.new_chat_member.status
        
        logger.info(f"👤 Usuario {member_update.new_chat_member.user.id}: {old_status} -> {new_status}")
        
        # Usuario se une al grupo
        if (old_status in [ChatMember.LEFT, ChatMember.KICKED, "unknown"] and 
            new_status == ChatMember.MEMBER):
            
            user = member_update.new_chat_member.user
            user_id = user.id
            username = user.username or f"id:{user_id}"
            chat_id = member_update.chat.id
            join_date = datetime.datetime.now(datetime.timezone.utc).isoformat()

            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO members (user_id, chat_id, join_date)
                VALUES (?, ?, ?)
            ''', (user_id, chat_id, join_date))
            conn.commit()
            conn.close()

            logger.info(f"📥 Usuario nuevo: @{username} agregado el {join_date}")
            
            # Actualizar estadísticas
            bot_status["members_count"] = get_stats()["total_members"]
            
    except Exception as e:
        logger.error(f"Error en handle_chat_member_update: {e}")
        bot_status["errors"].append(f"Error manejando usuario: {str(e)}")

# 🧪 Comando de prueba
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot funcionando correctamente con webhook!")
    logger.info(f"🧪 Comando /test ejecutado por {update.effective_user.id}")

# 📊 Comando de estado
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    message = f"🤖 Bot funcionando con webhook\n👥 Usuarios registrados: {stats['total_members']}\n📱 Grupos: {len(stats['groups'])}"
    await update.message.reply_text(message)

# 🔔 Comando para que el admin se registre
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == ADMIN_CHAT_ID:
        bot_status["admin_notified"] = True
        await update.message.reply_text("✅ ¡Hola Admin! Ahora puedo enviarte notificaciones.")
        logger.info("✅ Admin registrado para notificaciones")
    else:
        await update.message.reply_text("🤖 Bot de expulsión automática funcionando con webhook.")

# 🚫 Función para expulsar usuarios viejos
async def expel_old_user(user_id, chat_id, time_limit):
    try:
        bot = Bot(TOKEN)
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        logger.info(f"🧼 Usuario {user_id} expulsado del grupo {chat_id}")
        
        # Eliminar de la base de datos
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM members WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
        conn.commit()
        conn.close()
        
        # Notificar al admin si está registrado
        if bot_status["admin_notified"]:
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID, 
                    text=f"🧼 Usuario {user_id} expulsado por tiempo límite ({time_limit}s)"
                )
            except Exception as e:
                logger.warning(f"No se pudo notificar al admin: {e}")
                
    except Exception as e:
        logger.error(f"⚠️ Error expulsando a {user_id}: {e}")
        bot_status["errors"].append(f"Error expulsando {user_id}: {str(e)}")

# 🔄 Verificación periódica de miembros (ejecuta en background)
@run_async
async def check_old_members():
    logger.info("🔍 Verificando miembros para expulsión...")
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, chat_id, join_date FROM members')
        rows = cursor.fetchall()
        conn.close()

        logger.info(f"🔍 Verificando {len(rows)} miembros registrados...")
        bot_status["last_check"] = now.isoformat()
        bot_status["members_count"] = len(rows)

        time_limit = int(os.getenv("TIME_LIMIT_SECONDS", "120"))
        
        for user_id, chat_id, join_date in rows:
            joined = datetime.datetime.fromisoformat(join_date)
            seconds_in_group = (now - joined).total_seconds()
            
            if seconds_in_group >= time_limit:
                await expel_old_user(user_id, chat_id, time_limit)
                
    except Exception as e:
        logger.error(f"⚠️ Error en verificación de miembros: {e}")
        bot_status["errors"].append(f"Error verificación: {str(e)}")

# 🌐 Configurar webhook
@run_async
async def setup_webhook():
    try:
        bot = Bot(TOKEN)
        
        # Obtener información del bot
        bot_info = await bot.get_me()
        logger.info(f"✅ Bot conectado: @{bot_info.username} (ID: {bot_info.id})")
        
        # Configurar webhook si se proporciona URL
        if WEBHOOK_URL:
            webhook_url = f"{WEBHOOK_URL}/webhook/{TOKEN}"
            await bot.set_webhook(url=webhook_url)
            logger.info(f"✅ Webhook configurado: {webhook_url}")
            bot_status["webhook_set"] = True
        else:
            logger.warning("⚠️ WEBHOOK_URL no configurada")
            
        bot_status["running"] = True
        
    except Exception as e:
        logger.error(f"Error configurando webhook: {e}")
        bot_status["errors"].append(f"Error webhook: {str(e)}")

# 🌐 Rutas de Flask

@app.route('/')
def home():
    stats = get_stats()
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot de Telegram - Webhook</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .container { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 800px; margin: 0 auto; }
            .status { padding: 15px; border-radius: 5px; margin: 10px 0; }
            .running { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .stopped { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .warning { background: #fff3cd; color: #856404; border: 1px solid #ffeaa7; }
            .stats { background: #e2e3e5; padding: 15px; border-radius: 5px; margin: 10px 0; }
            .button { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; margin: 5px; text-decoration: none; display: inline-block; }
            .button:hover { background: #0056b3; }
            .button.danger { background: #dc3545; }
            .button.danger:hover { background: #c82333; }
        </style>
        <script>
            function refreshPage() { location.reload(); }
            setInterval(refreshPage, 30000);
        </script>
    </head>
    <body>
        <div class="container">
            <h1>🤖 Bot de Telegram - Webhook Mode</h1>
            
            <div class="status {{ 'running' if bot_running else 'stopped' }}">
                <strong>Estado del Bot:</strong> {{ '🟢 Funcionando con Webhook' if bot_running else '🔴 Detenido' }}
            </div>
            
            <div class="status {{ 'running' if webhook_set else 'warning' }}">
                <strong>Webhook:</strong> {{ '✅ Configurado' if webhook_set else '⚠️ No configurado' }}
            </div>
            
            {% if not admin_notified %}
            <div class="status warning">
                <strong>⚠️ Acción Requerida:</strong> Envía <code>/start</code> al bot en Telegram para recibir notificaciones.
            </div>
            {% endif %}
            
            <div class="stats">
                <h3>📊 Estadísticas</h3>
                <p><strong>👥 Total de usuarios:</strong> {{ total_members }}</p>
                <p><strong>📱 Grupos activos:</strong> {{ groups_count }}</p>
                <p><strong>🕐 Última verificación:</strong> {{ last_check or 'Nunca' }}</p>
                <p><strong>⏱️ Tiempo límite:</strong> {{ time_limit }} segundos</p>
                <p><strong>📬 Admin notificado:</strong> {{ '✅ Sí' if admin_notified else '❌ No' }}</p>
            </div>
            
            <div class="stats">
                <h3>🔧 Acciones</h3>
                <a href="/check_members" class="button">🔍 Verificar Miembros Ahora</a>
                <a href="/setup_webhook" class="button">🔗 Reconfigurar Webhook</a>
            </div>
            
            {% if errors %}
            <div class="stats">
                <h3>⚠️ Errores Recientes</h3>
                {% for error in errors[-5:] %}
                <p style="color: #721c24;">• {{ error }}</p>
                {% endfor %}
            </div>
            {% endif %}
            
            <div class="stats">
                <h3>🔗 API Endpoints</h3>
                <ul>
                    <li><a href="/status">/status</a> - Estado del bot (JSON)</li>
                    <li><a href="/stats">/stats</a> - Estadísticas (JSON)</li>
                    <li><a href="/health">/health</a> - Health check</li>
                </ul>
            </div>
            
            <button class="button" onclick="refreshPage()">🔄 Actualizar</button>
        </div>
    </body>
    </html>
    '''
    
    return render_template_string(html, 
        bot_running=bot_status["running"],
        webhook_set=bot_status["webhook_set"],
        total_members=stats["total_members"],
        groups_count=len(stats["groups"]),
        last_check=bot_status["last_check"],
        time_limit=os.getenv("TIME_LIMIT_SECONDS", "120"),
        admin_notified=bot_status["admin_notified"],
        errors=bot_status["errors"]
    )

@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    try:
        # Recibir actualización de Telegram
        json_data = request.get_json()
        
        if not json_data:
            return "No data", 400
            
        logger.info(f"📨 Webhook recibido: {json_data}")
        
        # Crear objeto Update
        update = Update.de_json(json_data, Bot(TOKEN))
        
        # Procesar la actualización
        if update.chat_member:
            # Ejecutar handler en thread separado
            def process_update():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    # Crear contexto mock
                    class MockContext:
                        bot = Bot(TOKEN)
                    
                    context = MockContext()
                    loop.run_until_complete(handle_chat_member_update(update, context))
                finally:
                    loop.close()
            
            thread = threading.Thread(target=process_update)
            thread.start()
            
        elif update.message:
            # Procesar comandos
            def process_command():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    class MockContext:
                        bot = Bot(TOKEN)
                    
                    context = MockContext()
                    
                    if update.message.text == "/start":
                        loop.run_until_complete(start_command(update, context))
                    elif update.message.text == "/test":
                        loop.run_until_complete(test_command(update, context))
                    elif update.message.text == "/status":
                        loop.run_until_complete(status_command(update, context))
                finally:
                    loop.close()
            
            thread = threading.Thread(target=process_command)
            thread.start()
        
        return "OK", 200
        
    except Exception as e:
        logger.error(f"Error procesando webhook: {e}")
        bot_status["errors"].append(f"Error webhook: {str(e)}")
        return "Error", 500

@app.route('/setup_webhook')
def setup_webhook_route():
    setup_webhook()
    return jsonify({"message": "Webhook reconfigurado", "status": "ok"})

@app.route('/check_members')
def check_members_route():
    check_old_members()
    return jsonify({"message": "Verificación de miembros ejecutada", "status": "ok"})

@app.route('/status')
def status():
    return jsonify({
        "bot_running": bot_status["running"],
        "webhook_set": bot_status["webhook_set"],
        "last_check": bot_status["last_check"],
        "members_count": bot_status["members_count"],
        "time_limit": int(os.getenv("TIME_LIMIT_SECONDS", "120")),
        "admin_notified": bot_status["admin_notified"],
        "errors": bot_status["errors"][-10:]
    })

@app.route('/stats')
def stats():
    return jsonify(get_stats())

@app.route('/health')
def health():
    return jsonify({
        "status": "ok", 
        "timestamp": datetime.datetime.now().isoformat(),
        "bot_running": bot_status["running"],
        "webhook_set": bot_status["webhook_set"]
    })

# 🚀 Inicialización
if __name__ == '__main__':
    logger.info("🚀 Iniciando aplicación con webhook...")
    
    # Inicializar base de datos
    init_db()
    
    # Configurar webhook
    setup_webhook()
    
    # Iniciar Flask
    logger.info(f"🌐 Iniciando servidor Flask en puerto {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
