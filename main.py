import sqlite3
import datetime
import asyncio
import os
import threading
from flask import Flask, jsonify, render_template_string
from dotenv import load_dotenv
from telegram import Update, ChatMember, Bot
from telegram.ext import ApplicationBuilder, ChatMemberHandler, ContextTypes, CommandHandler
import nest_asyncio
import logging

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

# 🌐 Crear aplicación Flask
app = Flask(__name__)

# Variable global para el bot
telegram_app = None
bot_status = {"running": False, "last_check": None, "members_count": 0}

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

# 🧪 Comando de prueba
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot funcionando correctamente!")
    logger.info(f"🧪 Comando /test ejecutado por {update.effective_user.id}")

# 📊 Comando de estado
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    message = f"🤖 Bot funcionando\n👥 Usuarios registrados: {stats['total_members']}\n📱 Grupos: {len(stats['groups'])}"
    await update.message.reply_text(message)

# 🚫 Expulsión de usuarios luego de cierto tiempo
async def check_old_members(app):
    logger.info("🔄 Iniciando verificación periódica de miembros...")
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, chat_id, join_date FROM members')
            rows = cursor.fetchall()

            logger.info(f"🔍 Verificando {len(rows)} miembros registrados...")
            bot_status["last_check"] = now.isoformat()
            bot_status["members_count"] = len(rows)

            for user_id, chat_id, join_date in rows:
                joined = datetime.datetime.fromisoformat(join_date)
                seconds_in_group = (now - joined).total_seconds()
                
                time_limit = int(os.getenv("TIME_LIMIT_SECONDS", "120"))
                
                if seconds_in_group >= time_limit:
                    try:
                        await app.bot.ban_chat_member(chat_id, user_id)
                        await app.bot.unban_chat_member(chat_id, user_id)
                        logger.info(f"🧼 Usuario {user_id} expulsado del grupo {chat_id}")
                        
                        cursor.execute('DELETE FROM members WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
                        conn.commit()
                        
                        # Notificar al admin
                        try:
                            await app.bot.send_message(
                                chat_id=ADMIN_CHAT_ID, 
                                text=f"🧼 Usuario {user_id} expulsado por tiempo límite ({time_limit}s)"
                            )
                        except Exception as e:
                            logger.error(f"Error enviando notificación: {e}")
                            
                    except Exception as e:
                        logger.error(f"⚠️ Error expulsando a {user_id}: {e}")
            
            conn.close()
        except Exception as e:
            logger.error(f"⚠️ Error en verificación de miembros: {e}")
        
        await asyncio.sleep(30)

# 🤖 Función principal del bot
async def run_telegram_bot():
    global telegram_app, bot_status
    
    try:
        logger.info("🚀 Iniciando bot de Telegram...")
        init_db()
        
        # Verificar conexión con Telegram
        bot = Bot(TOKEN)
        bot_info = await bot.get_me()
        logger.info(f"✅ Bot conectado: @{bot_info.username} (ID: {bot_info.id})")
        
        telegram_app = ApplicationBuilder().token(TOKEN).build()
        
        # Añadir handlers
        telegram_app.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
        telegram_app.add_handler(CommandHandler("test", test_command))
        telegram_app.add_handler(CommandHandler("status", status_command))
        
        # Ejecutar verificación en segundo plano
        asyncio.create_task(check_old_members(telegram_app))
        
        bot_status["running"] = True
        
        # Enviar mensaje de inicio
        try:
            await telegram_app.bot.send_message(
                chat_id=ADMIN_CHAT_ID, 
                text="🤖 Bot iniciado correctamente ✅"
            )
        except Exception as e:
            logger.error(f"No se pudo enviar mensaje al admin: {e}")
        
        # Iniciar polling
        await telegram_app.run_polling(
            allowed_updates=["chat_member", "message"],
            drop_pending_updates=True
        )
        
    except Exception as e:
        logger.error(f"Error en el bot de Telegram: {e}")
        bot_status["running"] = False

# 🌐 Rutas de Flask
@app.route('/')
def home():
    stats = get_stats()
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot de Telegram - Estado</title>
        <meta charset="utf-8">
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }
            .container { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            .status { padding: 15px; border-radius: 5px; margin: 10px 0; }
            .running { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .stopped { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .stats { background: #e2e3e5; padding: 15px; border-radius: 5px; margin: 10px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 Bot de Telegram - Panel de Control</h1>
            
            <div class="status {{ 'running' if bot_running else 'stopped' }}">
                <strong>Estado del Bot:</strong> {{ '🟢 Funcionando' if bot_running else '🔴 Detenido' }}
            </div>
            
            <div class="stats">
                <h3>📊 Estadísticas</h3>
                <p><strong>👥 Total de usuarios:</strong> {{ total_members }}</p>
                <p><strong>📱 Grupos activos:</strong> {{ groups_count }}</p>
                <p><strong>🕐 Última verificación:</strong> {{ last_check or 'Nunca' }}</p>
                <p><strong>⏱️ Tiempo límite:</strong> {{ time_limit }} segundos</p>
            </div>
            
            <div class="stats">
                <h3>🔗 Endpoints disponibles</h3>
                <ul>
                    <li><a href="/status">/status</a> - Estado del bot (JSON)</li>
                    <li><a href="/stats">/stats</a> - Estadísticas (JSON)</li>
                    <li><a href="/health">/health</a> - Health check</li>
                </ul>
            </div>
        </div>
    </body>
    </html>
    '''
    
    return render_template_string(html, 
        bot_running=bot_status["running"],
        total_members=stats["total_members"],
        groups_count=len(stats["groups"]),
        last_check=bot_status["last_check"],
        time_limit=os.getenv("TIME_LIMIT_SECONDS", "120")
    )

@app.route('/status')
def status():
    return jsonify({
        "bot_running": bot_status["running"],
        "last_check": bot_status["last_check"],
        "members_count": bot_status["members_count"],
        "time_limit": int(os.getenv("TIME_LIMIT_SECONDS", "120"))
    })

@app.route('/stats')
def stats():
    return jsonify(get_stats())

@app.route('/health')
def health():
    return jsonify({"status": "ok", "timestamp": datetime.datetime.now().isoformat()})

# 🚀 Función para ejecutar el bot en un hilo separado
def start_telegram_bot():
    nest_asyncio.apply()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_telegram_bot())

# 🌟 Punto de entrada principal
if __name__ == '__main__':
    # Iniciar el bot de Telegram en un hilo separado
    bot_thread = threading.Thread(target=start_telegram_bot, daemon=True)
    bot_thread.start()
    
    # Iniciar Flask
    logger.info(f"🌐 Iniciando servidor Flask en puerto {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
