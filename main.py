import sqlite3
import datetime
import os
import json
import time
from flask import Flask, request, jsonify, render_template_string
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes
import logging
import asyncio
import threading
from functools import wraps

# Configurar logging más detallado
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# ⏰ Variables de tiempo configurables
TIME_LIMIT_SECONDS = int(os.getenv("TIME_LIMIT_SECONDS", "120"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "120"))

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
    "webhook_set": False,
    "last_webhook_update": None,
    "next_check": None,
    "auto_check_running": False,
    "total_expelled": 0,
    "webhook_events_received": 0,
    "members_detected": 0
}

# Control del hilo de verificación automática
auto_check_thread = None
stop_auto_check = threading.Event()

# 🧱 Inicializar DB
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS members (
            user_id INTEGER,
            chat_id INTEGER,
            join_date TEXT,
            username TEXT,
            first_name TEXT,
            PRIMARY KEY (user_id, chat_id)
        )
    ''')
    
    # Tabla para estadísticas de expulsiones
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expulsions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER,
            username TEXT,
            first_name TEXT,
            expelled_date TEXT,
            time_in_group_seconds INTEGER
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
        
        # Miembros actuales
        cursor.execute('SELECT COUNT(*) FROM members')
        total_members = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT chat_id, COUNT(*) as count 
            FROM members 
            GROUP BY chat_id
        ''')
        groups = cursor.fetchall()
        
        # Miembros recientes
        cursor.execute('''
            SELECT user_id, username, first_name, join_date, chat_id
            FROM members 
            ORDER BY join_date DESC 
            LIMIT 10
        ''')
        recent_members = cursor.fetchall()
        
        # Expulsiones totales
        cursor.execute('SELECT COUNT(*) FROM expulsions')
        total_expelled = cursor.fetchone()[0]
        
        # Expulsiones recientes
        cursor.execute('''
            SELECT user_id, username, first_name, expelled_date, time_in_group_seconds, chat_id
            FROM expulsions 
            ORDER BY expelled_date DESC 
            LIMIT 5
        ''')
        recent_expulsions = cursor.fetchall()
        
        conn.close()
        
        return {
            "total_members": total_members,
            "total_expelled": total_expelled,
            "groups": [{"chat_id": chat_id, "members": count} for chat_id, count in groups],
            "recent_members": [
                {
                    "user_id": user_id, 
                    "username": username or f"id_{user_id}", 
                    "first_name": first_name or "Sin nombre",
                    "join_date": join_date,
                    "chat_id": chat_id
                } 
                for user_id, username, first_name, join_date, chat_id in recent_members
            ],
            "recent_expulsions": [
                {
                    "user_id": user_id,
                    "username": username or f"id_{user_id}",
                    "first_name": first_name or "Sin nombre",
                    "expelled_date": expelled_date,
                    "time_in_group_seconds": time_in_group_seconds,
                    "chat_id": chat_id
                }
                for user_id, username, first_name, expelled_date, time_in_group_seconds, chat_id in recent_expulsions
            ]
        }
    except Exception as e:
        logger.error(f"Error obteniendo estadísticas: {e}")
        return {"total_members": 0, "total_expelled": 0, "groups": [], "recent_members": [], "recent_expulsions": []}

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

# 📥 Manejo de usuarios que se unen - CORREGIDO
async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info("🔍 Procesando actualización de chat_member...")
        
        member_update = update.chat_member
        if not member_update:
            logger.warning("⚠️ No hay información de chat_member en la actualización")
            return
        
        # Obtener información del usuario
        user = member_update.new_chat_member.user
        user_id = user.id
        username = user.username or None
        first_name = user.first_name or "Sin nombre"
        chat_id = member_update.chat.id
        
        # Obtener estados
        old_status = member_update.old_chat_member.status if member_update.old_chat_member else "unknown"
        new_status = member_update.new_chat_member.status
        
        logger.info(f"👤 DETECCIÓN DE CAMBIO DE ESTADO:")
        logger.info(f"   Usuario: {user_id} (@{username}) - {first_name}")
        logger.info(f"   Chat: {chat_id}")
        logger.info(f"   Estado: {old_status} -> {new_status}")
        
        # CONDICIONES AMPLIADAS para detectar nuevos miembros
        is_new_member = False
        
        # Caso 1: Usuario se une por primera vez
        if old_status in ["left", "kicked", "unknown", None] and new_status == "member":
            is_new_member = True
            logger.info("✅ CASO 1: Usuario se une por primera vez")
        
        # Caso 2: Usuario era "left" y ahora es "member"
        elif old_status == "left" and new_status == "member":
            is_new_member = True
            logger.info("✅ CASO 2: Usuario regresa al grupo")
        
        # Caso 3: Usuario no tenía estado previo y ahora es member
        elif not member_update.old_chat_member and new_status == "member":
            is_new_member = True
            logger.info("✅ CASO 3: Usuario nuevo sin estado previo")
        
        if is_new_member:
            join_date = datetime.datetime.now(datetime.timezone.utc).isoformat()
            
            # Guardar en base de datos
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO members (user_id, chat_id, join_date, username, first_name)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, chat_id, join_date, username, first_name))
            conn.commit()
            conn.close()

            # Actualizar contadores
            bot_status["members_detected"] += 1
            bot_status["members_count"] = get_stats()["total_members"]
            
            logger.info(f"📥 ✅ NUEVO MIEMBRO REGISTRADO:")
            logger.info(f"   👤 Usuario: @{username} ({first_name})")
            logger.info(f"   🆔 ID: {user_id}")
            logger.info(f"   📱 Chat: {chat_id}")
            logger.info(f"   📅 Fecha: {join_date}")
            logger.info(f"   📊 Total miembros: {bot_status['members_count']}")
            
            # Notificar al admin si está registrado
            if bot_status["admin_notified"]:
                try:
                    bot = Bot(TOKEN)
                    notification_text = f"""📥 NUEVO MIEMBRO DETECTADO

👤 Usuario: @{username or 'sin_username'} ({first_name})
🆔 ID: {user_id}
📱 Chat: {chat_id}
⏰ Será expulsado en {TIME_LIMIT_SECONDS} segundos
📅 Fecha: {join_date[:19]}

📊 Total miembros activos: {bot_status['members_count']}"""
                    
                    await bot.send_message(chat_id=ADMIN_CHAT_ID, text=notification_text)
                    logger.info("📬 Notificación enviada al admin")
                except Exception as e:
                    logger.warning(f"No se pudo notificar nuevo miembro: {e}")
            
        # Usuario sale del grupo
        elif old_status == "member" and new_status in ["left", "kicked"]:
            # Eliminar de la base de datos
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM members WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
            deleted_rows = cursor.rowcount
            conn.commit()
            conn.close()
            
            if deleted_rows > 0:
                logger.info(f"👋 Usuario {user_id} (@{username}) salió del grupo {chat_id} - Eliminado de BD")
                bot_status["members_count"] = get_stats()["total_members"]
            else:
                logger.info(f"👋 Usuario {user_id} salió pero no estaba en BD")
        
        else:
            logger.info(f"ℹ️ Cambio de estado no relevante: {old_status} -> {new_status}")
            
    except Exception as e:
        error_msg = f"Error en handle_chat_member_update: {e}"
        logger.error(error_msg)
        bot_status["errors"].append(error_msg)

# 🧪 Comando de prueba
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    message = f"""✅ Bot funcionando correctamente!

📊 Estadísticas:
👥 Usuarios registrados: {stats['total_members']}
🧼 Total expulsados: {stats['total_expelled']}
📨 Webhooks recibidos: {bot_status['webhook_events_received']}
👤 Miembros detectados: {bot_status['members_detected']}

⏰ Configuración:
🔄 Verificación cada: {CHECK_INTERVAL_SECONDS}s
⏱️ Expulsión en: {TIME_LIMIT_SECONDS}s"""
    
    await update.message.reply_text(message)
    logger.info(f"🧪 Comando /test ejecutado por {update.effective_user.id}")

# 📊 Comando de estado
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_stats()
    message = f"""🤖 Estado del Bot

📊 Estadísticas:
👥 Usuarios activos: {stats['total_members']}
📱 Grupos: {len(stats['groups'])}
🧼 Total expulsados: {stats['total_expelled']}

📨 Eventos:
🔗 Webhooks recibidos: {bot_status['webhook_events_received']}
👤 Miembros detectados: {bot_status['members_detected']}"""
    
    if stats['recent_members']:
        message += "\n\n📋 Últimos miembros:"
        for member in stats['recent_members'][:3]:
            message += f"\n• @{member['username']} - {member['join_date'][:16]}"
    
    await update.message.reply_text(message)

# 🔔 Comando para que el admin se registre
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == ADMIN_CHAT_ID:
        bot_status["admin_notified"] = True
        await update.message.reply_text(f"""✅ ¡Hola Admin! Bot configurado correctamente.

⏰ Configuración actual:
• Tiempo de expulsión: {TIME_LIMIT_SECONDS}s
• Verificación cada: {CHECK_INTERVAL_SECONDS}s
• Notificaciones: ✅ Activadas

📊 Estadísticas:
• Webhooks recibidos: {bot_status['webhook_events_received']}
• Miembros detectados: {bot_status['members_detected']}""")
        logger.info("✅ Admin registrado para notificaciones")
    else:
        await update.message.reply_text("🤖 Bot de expulsión automática funcionando con webhook.")

# 🚫 Función para expulsar usuarios viejos
async def expel_old_user(user_id, chat_id, time_limit, username, first_name, time_in_group):
    try:
        bot = Bot(TOKEN)
        
        # Verificar permisos del bot
        try:
            chat_member = await bot.get_chat_member(chat_id, bot.id)
            if not chat_member.can_restrict_members:
                logger.warning(f"⚠️ Bot no tiene permisos para expulsar en chat {chat_id}")
                return False
        except Exception as e:
            logger.warning(f"No se pudo verificar permisos: {e}")
        
        # Expulsar usuario
        await bot.ban_chat_member(chat_id, user_id)
        await bot.unban_chat_member(chat_id, user_id)
        logger.info(f"🧼 Usuario {user_id} (@{username}) expulsado del grupo {chat_id}")
        
        # Registrar expulsión en la base de datos
        expelled_date = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # Eliminar de miembros activos
        cursor.execute('DELETE FROM members WHERE user_id = ? AND chat_id = ?', (user_id, chat_id))
        
        # Registrar en historial de expulsiones
        cursor.execute('''
            INSERT INTO expulsions (user_id, chat_id, username, first_name, expelled_date, time_in_group_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, chat_id, username, first_name, expelled_date, int(time_in_group)))
        
        conn.commit()
        conn.close()
        
        # Actualizar contador
        bot_status["total_expelled"] += 1
        
        # Notificar al admin si está registrado
        if bot_status["admin_notified"]:
            try:
                notification_text = f"""🧼 USUARIO EXPULSADO AUTOMÁTICAMENTE

👤 Usuario: @{username or 'sin_username'} ({first_name})
🆔 ID: {user_id}
⏱️ Tiempo en grupo: {int(time_in_group)}s
🎯 Límite configurado: {time_limit}s
📱 Chat: {chat_id}
📅 Fecha expulsión: {expelled_date[:19]}

📊 Total expulsados: {bot_status['total_expelled']}"""
                
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text=notification_text)
            except Exception as e:
                logger.warning(f"No se pudo notificar al admin: {e}")
        
        return True
                
    except Exception as e:
        logger.error(f"⚠️ Error expulsando a {user_id}: {e}")
        bot_status["errors"].append(f"Error expulsando {user_id}: {str(e)}")
        return False

# 🔄 Verificación de miembros (función principal)
async def check_old_members_async():
    logger.info("🔍 Verificando miembros para expulsión...")
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, chat_id, join_date, username, first_name FROM members')
        rows = cursor.fetchall()
        conn.close()

        logger.info(f"🔍 Verificando {len(rows)} miembros registrados...")
        bot_status["last_check"] = now.isoformat()
        bot_status["members_count"] = len(rows)

        expelled_count = 0
        
        for user_id, chat_id, join_date, username, first_name in rows:
            joined = datetime.datetime.fromisoformat(join_date)
            seconds_in_group = (now - joined).total_seconds()
            
            logger.info(f"⏳ Usuario {user_id} (@{username or 'sin_username'}) lleva {seconds_in_group:.1f}s en el grupo (límite: {TIME_LIMIT_SECONDS}s)")
            
            if seconds_in_group >= TIME_LIMIT_SECONDS:
                success = await expel_old_user(user_id, chat_id, TIME_LIMIT_SECONDS, username, first_name, seconds_in_group)
                if success:
                    expelled_count += 1
        
        if expelled_count > 0:
            logger.info(f"🧼 Total de usuarios expulsados en esta verificación: {expelled_count}")
        else:
            logger.info("✅ No hay usuarios para expulsar en este momento")
                
    except Exception as e:
        logger.error(f"⚠️ Error en verificación de miembros: {e}")
        bot_status["errors"].append(f"Error verificación: {str(e)}")

# 🔄 Wrapper para ejecutar verificación manual
@run_async
async def check_old_members():
    await check_old_members_async()

# 🤖 Verificación automática en background
def auto_check_members():
    """Función que ejecuta la verificación automática cada X segundos"""
    logger.info(f"🔄 Iniciando verificación automática cada {CHECK_INTERVAL_SECONDS} segundos...")
    bot_status["auto_check_running"] = True
    
    while not stop_auto_check.is_set():
        try:
            # Calcular próxima verificación
            next_check_time = datetime.datetime.now() + datetime.timedelta(seconds=CHECK_INTERVAL_SECONDS)
            bot_status["next_check"] = next_check_time.isoformat()
            
            # Esperar el intervalo configurado
            if stop_auto_check.wait(CHECK_INTERVAL_SECONDS):
                break  # Si se solicita parar, salir del bucle
            
            # Ejecutar verificación
            logger.info("🔄 Ejecutando verificación automática...")
            
            # Ejecutar la verificación de forma asíncrona
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(check_old_members_async())
            finally:
                loop.close()
                
        except Exception as e:
            logger.error(f"Error en verificación automática: {e}")
            bot_status["errors"].append(f"Error auto-verificación: {str(e)}")
    
    bot_status["auto_check_running"] = False
    logger.info("🛑 Verificación automática detenida")

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
    
    # Calcular tiempo restante para próxima verificación
    next_check_in = "Calculando..."
    if bot_status["next_check"]:
        try:
            next_check_time = datetime.datetime.fromisoformat(bot_status["next_check"])
            now = datetime.datetime.now()
            time_diff = (next_check_time - now).total_seconds()
            if time_diff > 0:
                minutes = int(time_diff // 60)
                seconds = int(time_diff % 60)
                next_check_in = f"{minutes}m {seconds}s"
            else:
                next_check_in = "Ahora"
        except:
            next_check_in = "Error calculando"
    
    html = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot de Telegram - Debug Mode</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .container { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 1000px; margin: 0 auto; }
            .status { padding: 15px; border-radius: 5px; margin: 10px 0; }
            .running { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .stopped { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .warning { background: #fff3cd; color: #856404; border: 1px solid #ffeaa7; }
            .info { background: #d1ecf1; color: #0c5460; border: 1px solid #bee5eb; }
            .debug { background: #f8f9fa; color: #495057; border: 1px solid #dee2e6; }
            .stats { background: #e2e3e5; padding: 15px; border-radius: 5px; margin: 10px 0; }
            .button { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; margin: 5px; text-decoration: none; display: inline-block; }
            .button:hover { background: #0056b3; }
            .button.success { background: #28a745; }
            .button.success:hover { background: #218838; }
            .button.danger { background: #dc3545; }
            .button.danger:hover { background: #c82333; }
            .list { max-height: 200px; overflow-y: auto; background: #f8f9fa; padding: 10px; border-radius: 5px; }
            .item { padding: 5px; border-bottom: 1px solid #dee2e6; font-size: 14px; }
            .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
            @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
        </style>
        <script>
            function refreshPage() { location.reload(); }
            setInterval(refreshPage, 15000); // Refresh más frecuente para debug
        </script>
    </head>
    <body>
        <div class="container">
            <h1>🤖 Bot de Telegram - Debug Mode</h1>
            
            <div class="status {{ 'running' if bot_running else 'stopped' }}">
                <strong>Estado del Bot:</strong> {{ '🟢 Funcionando' if bot_running else '🔴 Detenido' }}
            </div>
            
            <div class="status {{ 'running' if webhook_set else 'warning' }}">
                <strong>Webhook:</strong> {{ '✅ Configurado' if webhook_set else '⚠️ No configurado' }}
            </div>
            
            <div class="status {{ 'running' if auto_check_running else 'warning' }}">
                <strong>Verificación Automática:</strong> {{ '🔄 Activa' if auto_check_running else '⚠️ Inactiva' }}
            </div>
            
            <div class="status debug">
                <strong>🔍 Debug Info:</strong><br>
                • Webhooks recibidos: <strong>{{ webhook_events_received }}</strong><br>
                • Miembros detectados: <strong>{{ members_detected }}</strong><br>
                • Última actualización webhook: {{ last_webhook_update or 'Nunca' }}
            </div>
            
            <div class="status info">
                <strong>⏰ Configuración:</strong><br>
                • Tiempo para expulsión: <strong>{{ time_limit }}s</strong> ({{ time_limit_minutes }})<br>
                • Verificación cada: <strong>{{ check_interval }}s</strong> ({{ check_interval_minutes }})<br>
                • Próxima verificación en: <strong>{{ next_check_in }}</strong>
            </div>
            
            {% if not admin_notified %}
            <div class="status warning">
                <strong>⚠️ Acción Requerida:</strong> Envía <code>/start</code> al bot en Telegram para recibir notificaciones.
            </div>
            {% endif %}
            
            <div class="stats">
                <h3>📊 Estadísticas</h3>
                <div class="grid">
                    <div>
                        <p><strong>👥 Usuarios activos:</strong> {{ total_members }}</p>
                        <p><strong>🧼 Total expulsados:</strong> {{ total_expelled }}</p>
                        <p><strong>📱 Grupos monitoreados:</strong> {{ groups_count }}</p>
                    </div>
                    <div>
                        <p><strong>🕐 Última verificación:</strong> {{ last_check or 'Nunca' }}</p>
                        <p><strong>📨 Eventos webhook:</strong> {{ webhook_events_received }}</p>
                        <p><strong>👤 Detecciones:</strong> {{ members_detected }}</p>
                    </div>
                </div>
            </div>
            
            <div class="grid">
                {% if recent_members %}
                <div class="stats">
                    <h3>👥 Miembros Recientes</h3>
                    <div class="list">
                        {% for member in recent_members %}
                        <div class="item">
                            <strong>@{{ member.username }}</strong> ({{ member.first_name }})<br>
                            📅 {{ member.join_date[:16] }}<br>
                            📱 Chat: {{ member.chat_id }}
                        </div>
                        {% endfor %}
                    </div>
                </div>
                {% endif %}
                
                {% if recent_expulsions %}
                <div class="stats">
                    <h3>🧼 Expulsiones Recientes</h3>
                    <div class="list">
                        {% for expulsion in recent_expulsions %}
                        <div class="item">
                            <strong>@{{ expulsion.username }}</strong> ({{ expulsion.first_name }})<br>
                            🧼 {{ expulsion.expelled_date[:16] }}<br>
                            ⏱️ Tiempo: {{ expulsion.time_in_group_seconds }}s<br>
                            📱 Chat: {{ expulsion.chat_id }}
                        </div>
                        {% endfor %}
                    </div>
                </div>
                {% endif %}
            </div>
            
            <div class="stats">
                <h3>🔧 Acciones</h3>
                <a href="/check_members" class="button success">🔍 Verificar Ahora</a>
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
            
            <button class="button" onclick="refreshPage()">🔄 Actualizar (Auto: 15s)</button>
        </div>
    </body>
    </html>
    '''
    
    return render_template_string(html, 
        bot_running=bot_status["running"],
        webhook_set=bot_status["webhook_set"],
        auto_check_running=bot_status["auto_check_running"],
        total_members=stats["total_members"],
        total_expelled=stats["total_expelled"],
        groups_count=len(stats["groups"]),
        recent_members=stats["recent_members"],
        recent_expulsions=stats["recent_expulsions"],
        last_check=bot_status["last_check"],
        last_webhook_update=bot_status["last_webhook_update"],
        webhook_events_received=bot_status["webhook_events_received"],
        members_detected=bot_status["members_detected"],
        time_limit=TIME_LIMIT_SECONDS,
        time_limit_minutes=f"{TIME_LIMIT_SECONDS//60}m {TIME_LIMIT_SECONDS%60}s",
        check_interval=CHECK_INTERVAL_SECONDS,
        check_interval_minutes=f"{CHECK_INTERVAL_SECONDS//60}m {CHECK_INTERVAL_SECONDS%60}s",
        next_check_in=next_check_in,
        admin_notified=bot_status["admin_notified"],
        errors=bot_status["errors"]
    )

@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    try:
        # Incrementar contador de webhooks recibidos
        bot_status["webhook_events_received"] += 1
        
        # Recibir actualización de Telegram
        json_data = request.get_json()
        
        if not json_data:
            logger.warning("⚠️ Webhook recibido sin datos")
            return "No data", 400
            
        logger.info(f"📨 WEBHOOK #{bot_status['webhook_events_received']} RECIBIDO:")
        logger.info(f"   Datos: {json.dumps(json_data, indent=2)}")
        
        bot_status["last_webhook_update"] = datetime.datetime.now().isoformat()
        
        # Crear objeto Update
        update = Update.de_json(json_data, Bot(TOKEN))
        
        # Procesar la actualización
        if update.chat_member:
            logger.info("🔍 Procesando actualización de chat_member...")
            
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
            logger.info("💬 Procesando mensaje/comando...")
            
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
        else:
            logger.info("ℹ️ Webhook recibido pero no contiene chat_member ni message")
        
        return "OK", 200
        
    except Exception as e:
        error_msg = f"Error procesando webhook: {e}"
        logger.error(error_msg)
        bot_status["errors"].append(error_msg)
        return "Error", 500

@app.route('/setup_webhook')
def setup_webhook_route():
    setup_webhook()
    return jsonify({"message": "Webhook reconfigurado", "status": "ok"})

@app.route('/check_members')
def check_members_route():
    logger.info("🔍 Verificación manual solicitada desde dashboard")
    check_old_members()
    return jsonify({"message": "Verificación manual ejecutada", "status": "ok"})

@app.route('/status')
def status():
    return jsonify({
        "bot_running": bot_status["running"],
        "webhook_set": bot_status["webhook_set"],
        "auto_check_running": bot_status["auto_check_running"],
        "last_check": bot_status["last_check"],
        "last_webhook_update": bot_status["last_webhook_update"],
        "next_check": bot_status["next_check"],
        "members_count": bot_status["members_count"],
        "total_expelled": bot_status["total_expelled"],
        "webhook_events_received": bot_status["webhook_events_received"],
        "members_detected": bot_status["members_detected"],
        "time_limit_seconds": TIME_LIMIT_SECONDS,
        "check_interval_seconds": CHECK_INTERVAL_SECONDS,
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
        "webhook_set": bot_status["webhook_set"],
        "auto_check_running": bot_status["auto_check_running"]
    })

# 🚀 Inicialización
if __name__ == '__main__':
    logger.info("🚀 Iniciando aplicación con verificación automática y debug...")
    
    # Inicializar base de datos
    init_db()
    
    # Configurar webhook
    setup_webhook()
    
    # Iniciar verificación automática en background
    auto_check_thread = threading.Thread(target=auto_check_members, daemon=True)
    auto_check_thread.start()
    
    # Iniciar Flask
    logger.info(f"🌐 Iniciando servidor Flask en puerto {PORT}")
    logger.info(f"⏰ Configuración: Expulsión en {TIME_LIMIT_SECONDS}s, Verificación cada {CHECK_INTERVAL_SECONDS}s")
    
    try:
        app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("🛑 Deteniendo aplicación...")
        stop_auto_check.set()
        if auto_check_thread:
            auto_check_thread.join()
