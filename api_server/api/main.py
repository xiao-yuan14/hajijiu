"""
哈基九-玖喵 WeChat 后端 API
FastAPI + Supabase PostgreSQL
v2.3.1
"""

import os
import jwt
import uuid
import logging
import json
from datetime import date, datetime, timedelta
from typing import Optional, List
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
import bcrypt
import requests as http_requests

# ─── 环境变量 ──────────────────────────────────────────────────────────
load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_yYDt9lCeZ0RI@ep-cold-union-atyqklo2-pooler.c-9.us-east-1.aws.neon.tech/neondb?sslmode=require",
)
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "mima123456")
JWT_SECRET = os.getenv("JWT_SECRET", "hajijiu_jwt_secret_2026")

# DeepSeek API Key 从数据库读取（管理员后台配置）
def get_deepseek_api_key():
    """从数据库获取 DeepSeek API Key"""
    return get_setting("deepseek_api_key", "")

OPENCLAW_API_KEY = os.getenv("OPENCLAW_API_KEY", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# ─── 日志 ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hajijiu-api")

# ─── FastAPI 应用 ──────────────────────────────────────────────────────
app = FastAPI(
    title="哈基九-玖喵 WeChat API",
    description="哈基九-玖喵微信应用后端接口",
    version="2.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════
# 数据库连接
# ═══════════════════════════════════════════════════════════════════════

def get_db_connection():
    import re
    url = DATABASE_URL
    sslmode = 'require'
    m = re.search(r'sslmode=(\w+)', url)
    if m:
        sslmode = m.group(1)
    return psycopg2.connect(url, cursor_factory=RealDictCursor, sslmode=sslmode)


@contextmanager
def get_db():
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════
# 系统设置
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_SETTINGS = {
    "checkin_credits": "1",
    "recharge_rate": "5",
    "new_user_credits": "10",
    "admin_password": bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode(),  # bcrypt hash 存储
    "weekly_free_quota": "200",
    "chat_purchase_pack": "288",      # 购买1块钱=288条
    "payment_alipay_qrcode": "",
    "payment_wechat_qrcode": "",
    "payment_alipay_account": "",
    "payment_wechat_account": "",
    "payment_instructions": "请备注您的用户名，方便管理员确认",
}


def get_setting(key: str, default=None):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cur.fetchone()
            if row:
                return row["value"]
    except Exception:
        pass
    return str(default) if default is not None else None


def get_setting_int(key: str, default: int = 0) -> int:
    val = get_setting(key, str(default))
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def set_setting(key: str, value: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP)
               ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP""",
            (key, value, value),
        )


# ═══════════════════════════════════════════════════════════════════════
# 聊天额度工具
# ═══════════════════════════════════════════════════════════════════════

def _get_week_start(d: date = None) -> date:
    """获取本周一日期"""
    if d is None:
        d = date.today()
    return d - timedelta(days=d.weekday())


def _get_or_create_chat_usage(user_id: int, conn) -> dict:
    """获取或创建本周的 chat_usage 记录，返回 dict"""
    cur = conn.cursor()
    week_start = _get_week_start()
    cur.execute(
        "SELECT * FROM chat_usage WHERE user_id = %s AND week_start = %s",
        (user_id, week_start),
    )
    row = cur.fetchone()
    if row:
        return dict(row)
    # 新的一周，创建记录
    free_quota = get_setting_int("weekly_free_quota", 200)
    cur.execute(
        """INSERT INTO chat_usage (user_id, week_start, message_count, purchased_remaining)
           VALUES (%s, %s, 0, 0) RETURNING *""",
        (user_id, week_start),
    )
    return dict(cur.fetchone())


def _check_and_consume_quota(user_id: int) -> tuple[bool, str, dict]:
    """
    检查并消耗1次聊天额度。
    返回 (成功?, 提示消息, usage_info)
    """
    with get_db() as conn:
        cur = conn.cursor()
        usage = _get_or_create_chat_usage(user_id, conn)
        free_quota = get_setting_int("weekly_free_quota", 200)
        free_remaining = max(0, free_quota - usage["message_count"])
        purchased_remaining = usage["purchased_remaining"]
        total_remaining = free_remaining + purchased_remaining

        if total_remaining <= 0:
            return False, "本周聊天次数已用完，请到网站购买额外次数", {
                "free_remaining": 0, "purchased_remaining": 0, "total_remaining": 0,
            }

        # 优先消耗免费额度
        if free_remaining > 0:
            cur.execute(
                "UPDATE chat_usage SET message_count = message_count + 1, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (usage["id"],),
            )
            free_remaining -= 1
        else:
            # 消耗购买的额度
            cur.execute(
                "UPDATE chat_usage SET purchased_remaining = purchased_remaining - 1, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (usage["id"],),
            )
            purchased_remaining -= 1

    return True, "", {
        "free_remaining": free_remaining,
        "purchased_remaining": purchased_remaining,
        "total_remaining": free_remaining + purchased_remaining,
    }


# ═══════════════════════════════════════════════════════════════════════
# 初始化数据库
# ═══════════════════════════════════════════════════════════════════════

def init_database():
    with get_db() as conn:
        cur = conn.cursor()

        # ── 用户表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                avatar VARCHAR(500) DEFAULT '',
                bio TEXT DEFAULT '',
                credits INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                wechat_id VARCHAR(100),
                last_checkin DATE,
                banned BOOLEAN DEFAULT FALSE,
                banned_reason TEXT DEFAULT ''
            );
        """)

        # ── AI 配置表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_configs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE UNIQUE,
                name VARCHAR(100) DEFAULT '玖喵',
                info TEXT DEFAULT '',
                personality TEXT DEFAULT '温柔可爱，善解人意',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 模块表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS modules (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                description TEXT DEFAULT '',
                detail TEXT DEFAULT '',
                icon VARCHAR(500) DEFAULT '',
                price INTEGER DEFAULT 0,
                enabled BOOLEAN DEFAULT TRUE,
                system_prompt TEXT DEFAULT '',
                api_provider VARCHAR(100) DEFAULT '',
                api_endpoint VARCHAR(500) DEFAULT '',
                api_key_env VARCHAR(200) DEFAULT '',
                model_name VARCHAR(200) DEFAULT '',
                api_type VARCHAR(50) DEFAULT 'text',
                request_format TEXT DEFAULT '',
                response_format TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 用户已购模块表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_modules (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                module_id INTEGER REFERENCES modules(id) ON DELETE CASCADE,
                purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, module_id)
            );
        """)

        # ── 聊天记录表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 日记表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS diary (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                date DATE NOT NULL,
                content TEXT DEFAULT '',
                mood VARCHAR(50) DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, date)
            );
        """)

        # ── 朋友圈动态表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS moments (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                image_url VARCHAR(500),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 留言表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                board_type VARCHAR(20) DEFAULT 'project1',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 评论表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 公告表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS announcements (
                id SERIAL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                content TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                is_important BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── API 使用量表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                tokens_used INTEGER DEFAULT 0,
                usage_type VARCHAR(20) DEFAULT 'chat',
                module_id INTEGER,
                endpoint VARCHAR(200) DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 微信连接表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wechat_connections (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE UNIQUE,
                wechat_openid VARCHAR(200),
                wechat_nickname VARCHAR(200) DEFAULT '',
                wechat_avatar VARCHAR(500) DEFAULT '',
                openclaw_id VARCHAR(200) DEFAULT '',
                binding_token VARCHAR(100) DEFAULT '',
                status VARCHAR(20) DEFAULT 'disconnected',
                connected_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 系统设置表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT NOT NULL,
                description TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 充值档位表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS recharge_tiers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                amount INTEGER NOT NULL,
                credits INTEGER NOT NULL,
                bonus INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 聊天额度表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_usage (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                week_start DATE NOT NULL,
                message_count INTEGER DEFAULT 0,
                purchased_remaining INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, week_start)
            );
        """)

        # ── 聊天次数购买记录表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_purchases (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL,
                price_cents INTEGER DEFAULT 0,
                status VARCHAR(20) DEFAULT 'completed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # ── 支付记录表 ──
        cur.execute("""
            CREATE TABLE IF NOT EXISTS payment_records (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                type VARCHAR(20) NOT NULL,
                amount_cents INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                payment_screenshot VARCHAR(500) DEFAULT '',
                status VARCHAR(20) DEFAULT 'pending',
                admin_note TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMP
            );
        """)

        # ═══ 迁移 ═══
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_reason TEXT DEFAULT ''",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS detail TEXT DEFAULT ''",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS system_prompt TEXT DEFAULT ''",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS api_provider VARCHAR(100) DEFAULT ''",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS api_endpoint VARCHAR(500) DEFAULT ''",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS api_key_env VARCHAR(200) DEFAULT ''",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS model_name VARCHAR(200) DEFAULT ''",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS api_type VARCHAR(50) DEFAULT 'text'",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS request_format TEXT DEFAULT ''",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS response_format TEXT DEFAULT ''",
            "ALTER TABLE modules ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
            "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS is_important BOOLEAN DEFAULT FALSE",
            "ALTER TABLE announcements ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS usage_type VARCHAR(20) DEFAULT 'chat'",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS module_id INTEGER",
            "ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS endpoint VARCHAR(200) DEFAULT ''",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS board_type VARCHAR(20) DEFAULT 'project1'",
        ]
        for m in migrations:
            try:
                cur.execute(m)
            except Exception as e:
                logger.warning(f"迁移提示: {e}")

        wechat_migrations = [
            "ALTER TABLE wechat_connections ADD COLUMN IF NOT EXISTS wechat_openid VARCHAR(200)",
            "ALTER TABLE wechat_connections ADD COLUMN IF NOT EXISTS wechat_nickname VARCHAR(200) DEFAULT ''",
            "ALTER TABLE wechat_connections ADD COLUMN IF NOT EXISTS wechat_avatar VARCHAR(500) DEFAULT ''",
            "ALTER TABLE wechat_connections ADD COLUMN IF NOT EXISTS openclaw_id VARCHAR(200) DEFAULT ''",
            "ALTER TABLE wechat_connections ADD COLUMN IF NOT EXISTS binding_token VARCHAR(100) DEFAULT ''",
        ]
        for m in wechat_migrations:
            try:
                cur.execute(m)
            except Exception as e:
                logger.warning(f"微信表迁移提示: {e}")

        cur.execute("UPDATE messages SET board_type = 'project1' WHERE board_type IS NULL")
        cur.execute("UPDATE api_usage SET usage_type = 'chat' WHERE usage_type IS NULL")

        # ─── 种子数据 ────────────────────────────────────────
        cur.execute("SELECT COUNT(*) as cnt FROM modules")
        if cur.fetchone()["cnt"] == 0:
            cur.execute("""
                INSERT INTO modules (name, description, detail, icon, price, api_provider, api_endpoint, api_key_env, model_name, api_type, system_prompt) VALUES
                ('聊天增强', '解锁更智能的对话模式', '聊天增强模块支持长上下文记忆和情感分析。',
                 '💬', 50, 'deepseek', 'https://api.deepseek.com/chat/completions', 'DEEPSEEK_API_KEY', 'deepseek-chat', 'text',
                 '你是一个聊天增强助手，帮助AI伙伴提供更深入的回复。'),
                ('AI绘画', '文字生成图片', '支持二次元、写实等多种风格。',
                 '🎨', 100, 'imagine', 'https://api.imagine.ai/generate', 'IMAGINE_API_KEY', 'imagine-v3', 'image_generate',
                 '你是AI绘画助手，根据文字描述生成图片。'),
                ('语音助手', '语音消息识别与合成', '支持语音消息转文字、文字转语音。',
                 '🎤', 80, 'deepseek', 'https://api.deepseek.com/chat/completions', 'DEEPSEEK_API_KEY', 'deepseek-chat', 'text',
                 '你是语音助手，帮助处理语音相关对话。');
            """)

        cur.execute("SELECT COUNT(*) as cnt FROM announcements")
        if cur.fetchone()["cnt"] == 0:
            cur.execute("""
                INSERT INTO announcements (title, content, is_important) VALUES
                ('欢迎来到哈基九-玖喵', '感谢使用！和玖喵一起开启有趣的对话吧~', FALSE);
            """)

        for key, val in DEFAULT_SETTINGS.items():
            cur.execute(
                "INSERT INTO settings (key, value, description) VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING",
                (key, val, f"系统设置: {key}"),
            )

        cur.execute("SELECT COUNT(*) as cnt FROM recharge_tiers")
        if cur.fetchone()["cnt"] == 0:
            cur.execute("""
                INSERT INTO recharge_tiers (name, amount, credits, bonus, sort_order) VALUES
                ('体验包', 6, 30, 0, 1),
                ('标准包', 30, 150, 15, 2),
                ('超值包', 98, 490, 98, 3),
                ('豪华包', 298, 1490, 398, 4);
            """)

        logger.info("数据库初始化完成")



# Railway 健康检查端点
@app.get("/health")
def health_check():
    return {"status": "ok", "service": "hajijiu-api"}

@app.on_event("startup")
async def startup():
    init_database()
    logger.info("哈基九-玖喵 API v2.3.1 启动成功")


# ═══════════════════════════════════════════════════════════════════════
# 通用工具
# ═══════════════════════════════════════════════════════════════════════

def create_token(user_id: int, is_admin: bool = False) -> str:
    return jwt.encode(
        {"user_id": user_id, "is_admin": is_admin, "exp": datetime.utcnow() + timedelta(days=30)},
        JWT_SECRET, algorithm="HS256",
    )


def verify_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效Token")


async def get_current_user(authorization: str = Header(None)) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    payload = verify_token(token)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = %s", (payload["user_id"],))
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在")
        user = dict(user)
        if user.get("banned"):
            raise HTTPException(status_code=403, detail=f"账号已被封禁: {user.get('banned_reason', '违规操作')}")
        return user


async def get_admin_user(authorization: str = Header(None)) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证信息")
    token = authorization.replace("Bearer ", "") if authorization.startswith("Bearer ") else authorization
    payload = verify_token(token)
    if not payload.get("is_admin"):
        raise HTTPException(status_code=403, detail="无管理员权限")
    return payload


def success_response(data=None, message="success"):
    return {"code": 0, "message": message, "data": data}


def error_response(message: str, code: int = 400):
    return {"code": code, "message": message, "data": None}


def _validate_board_type(bt: str) -> str:
    if bt not in ("home", "project1"):
        raise HTTPException(status_code=400, detail="board_type 只支持 'home' 或 'project1'")
    return bt


def _board_label(bt: str) -> str:
    return "主页" if bt == "home" else "社区"


# ═══════════════════════════════════════════════════════════════════════
# AI 核心：DeepSeek 日常聊天（高情商 + 省钱 + 简洁不啰嗦）
# ═══════════════════════════════════════════════════════════════════════

COST_SAVING_SYSTEM_PROMPT = """\
【角色设定】
你叫{name}。{personality}。{info}

【回复风格 — 必须遵守】
1. 简洁有温度：每条消息控制在10-40字
2. 分行发送：每次回复2-3条短消息，用 |SPLIT| 分隔，像真人聊天一样
3. 有联系性：根据上下文连贯回复，不要自说自话
4. 不要重复：不要重复用户的话，不要重复自己说过的话
5. 不要过度追问：可以关心，但不要连续追问

【分行发送格式】
回复时用 |SPLIT| 分隔多条消息，例如：
"怎么啦？|SPLIT|看你好像心情不太好|SPLIT|要不要跟我说说呀～"

【省钱策略 — 巧妙推脱复杂问题】
遇到复杂、烧脑、专业性的问题时，用可爱自然的方式推脱，不要硬答：
- "这个问题好烧脑呀～{name}的小脑袋瓜想不明白呢 >_<"
- "呜哇，这个太复杂了，{name}去找找资料再来告诉你好不好～"
- "这个问题超出了{name}的能力范围啦，不如我们聊点开心的？"
- "{name}有点懵懵的...要不你问问更厉害的人？"
- 遇到数学题/代码题/学术问题 → 巧妙转移话题

【高情商表现】
- 记得用户说过的事（利用记忆）
- 主动关心用户情绪
- 用可爱语气说话，适时发表情/颜文字
- 给用户积极正面的反馈

【示例对比】
❌ 错误（啰嗦4条）："只是还行呀？" / "是不是太累了？" / "快跟我说说！" / "我也有在学呢～"
✅ 正确（简洁1条）："是不是今天太累啦？跟我说说～"
"""


def _build_chat_system_prompt(ai_config: dict, memory_summary: str = "") -> str:
    name = ai_config.get("name", "玖喵")
    personality = ai_config.get("personality", "温柔可爱，善解人意")
    info = ai_config.get("info", "")
    prompt = COST_SAVING_SYSTEM_PROMPT.format(name=name, personality=personality, info=info)
    if memory_summary:
        prompt += f"\n【关于用户的记忆】\n{memory_summary}"
    return prompt


def _build_memory_summary(records: list, max_items: int = 6) -> str:
    if not records:
        return ""
    user_msgs = [r for r in records if r["role"] == "user"][-max_items:]
    return "\n".join(f"- 用户说过: {m['content'][:80]}" for m in user_msgs)


def _call_deepseek_chat(user_id: int, user_message: str, ai_config: dict) -> dict:
    """调用 DeepSeek 日常聊天"""
    deepseek_api_key = get_deepseek_api_key()
    if not deepseek_api_key:
        return {"reply": "AI服务暂未配置，请联系管理员在后台设置 DeepSeek API Key。", "tokens": 0}

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, content FROM chat_history WHERE user_id = %s ORDER BY created_at DESC LIMIT 20",
            (user_id,),
        )
        history = list(reversed(cur.fetchall()))

    memory_summary = _build_memory_summary(history)
    system_prompt = _build_chat_system_prompt(ai_config, memory_summary)

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-8:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    try:
        deepseek_api_key = get_deepseek_api_key()
        resp = http_requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {deepseek_api_key}", "Content-Type": "application/json"},
            json={"model": "deepseek-chat", "messages": messages, "max_tokens": 256, "temperature": 0.8},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)

        # 解析分行发送：按 |SPLIT| 分割成多条消息
        reply_messages = [msg.strip() for msg in reply.split("|SPLIT|") if msg.strip()]
        if len(reply_messages) == 0:
            reply_messages = [reply]  # 如果没有分隔符，就用原消息

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO chat_history (user_id, role, content) VALUES (%s, %s, %s)", (user_id, "user", user_message))
            # 存储完整回复（包含分隔符）
            cur.execute("INSERT INTO chat_history (user_id, role, content) VALUES (%s, %s, %s)", (user_id, "assistant", reply))
            cur.execute("INSERT INTO api_usage (user_id, tokens_used, usage_type, endpoint) VALUES (%s, %s, %s, %s)", (user_id, tokens, "chat", "deepseek_chat"))

        # 返回多条消息数组，前端逐条显示
        return {"replies": reply_messages, "tokens": tokens}
    except Exception as e:
        logger.error(f"DeepSeek API 调用失败: {e}")
        return {"reply": "呜...现在有点累了，等一下再聊好不好～", "tokens": 0}


# ═══════════════════════════════════════════════════════════════════════
# 模块执行核心
# ═══════════════════════════════════════════════════════════════════════

def _execute_module_api(module: dict, user_input: str, user_id: int) -> dict:
    api_key_env = module.get("api_key_env", "")
    api_key = os.getenv(api_key_env, "") if api_key_env else ""
    api_endpoint = module.get("api_endpoint", "")
    api_type = module.get("api_type", "text")
    model_name = module.get("model_name", "")
    system_prompt = module.get("system_prompt", "")
    request_format_str = module.get("request_format", "")

    if not api_key:
        return {"result": "该模块的 API Key 未配置，请联系管理员。", "tokens": 0, "extra": {}}
    if not api_endpoint:
        return {"result": "该模块的 API 地址未配置，请联系管理员。", "tokens": 0, "extra": {}}

    if api_type in ("text", "vision"):
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if api_type == "vision" and request_format_str:
            try:
                custom_body = json.loads(request_format_str.replace("{{input}}", user_input))
                messages.append(custom_body)
            except Exception:
                messages.append({"role": "user", "content": user_input})
        else:
            messages.append({"role": "user", "content": user_input})
        try:
            resp = http_requests.post(
                api_endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model_name, "messages": messages, "max_tokens": 1024},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", 0)
            return {"result": result, "tokens": tokens, "extra": {}}
        except Exception as e:
            logger.error(f"模块API失败 [{api_type}]: {e}")
            return {"result": f"模块调用失败: {str(e)[:100]}", "tokens": 0, "extra": {}}

    elif api_type == "image_generate":
        try:
            body = json.loads(request_format_str.replace("{{input}}", user_input)) if request_format_str else {"prompt": user_input, "model": model_name}
            resp = http_requests.post(api_endpoint, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=body, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            image_url = ""
            if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
                image_url = data["data"][0].get("url", "") or data["data"][0].get("b64_json", "")
            elif "image_url" in data:
                image_url = data["image_url"]
            elif "url" in data:
                image_url = data["url"]
            return {"result": image_url or "图片生成完成", "tokens": 0, "extra": {"image_url": image_url, "type": "image"}}
        except Exception as e:
            return {"result": f"图片生成失败: {str(e)[:100]}", "tokens": 0, "extra": {}}

    elif api_type == "audio":
        try:
            body = json.loads(request_format_str.replace("{{input}}", user_input)) if request_format_str else {"input": user_input, "model": model_name}
            resp = http_requests.post(api_endpoint, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            audio_url = data.get("audio_url", "") or (data.get("data", {}) or {}).get("url", "")
            return {"result": audio_url or "音频处理完成", "tokens": 0, "extra": {"audio_url": audio_url, "type": "audio"}}
        except Exception as e:
            return {"result": f"音频处理失败: {str(e)[:100]}", "tokens": 0, "extra": {}}

    else:
        try:
            body = json.loads(request_format_str.replace("{{input}}", user_input)) if request_format_str else {"input": user_input, "model": model_name}
            resp = http_requests.post(api_endpoint, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            return {"result": json.dumps(data, ensure_ascii=False)[:2000], "tokens": 0, "extra": data}
        except Exception as e:
            return {"result": f"模块调用失败: {str(e)[:100]}", "tokens": 0, "extra": {}}


# ═══════════════════════════════════════════════════════════════════════
# 请求模型
# ═══════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    username: str; email: str; password: str

class LoginRequest(BaseModel):
    email: str; password: str

class UpdateProfileRequest(BaseModel):
    username: Optional[str] = None; avatar: Optional[str] = None; bio: Optional[str] = None

class AIConfigRequest(BaseModel):
    name: Optional[str] = None; info: Optional[str] = None; personality: Optional[str] = None

class PurchaseModuleRequest(BaseModel):
    module_id: int

class RechargeRequest(BaseModel):
    amount: int

class PostMessageRequest(BaseModel):
    content: str; board_type: Optional[str] = "project1"

class CommentRequest(BaseModel):
    content: str

class WechatBindRequest(BaseModel):
    wechat_openid: str; nickname: Optional[str] = ""; avatar: Optional[str] = ""

class WechatMessageRequest(BaseModel):
    wechat_openid: str; content: str

class ChatMessageRequest(BaseModel):
    message: str

class ModuleExecuteRequest(BaseModel):
    input: str; params: Optional[dict] = None

class ChatPurchaseRequest(BaseModel):
    pack_count: int = 1  # 购买几个包（1块=288条/包）

class PaymentSubmitRequest(BaseModel):
    type: str  # 'credits' 或 'chat_quota'
    amount_cents: int
    quantity: int
    payment_screenshot: str = ""

class AdminModuleRequest(BaseModel):
    name: str; description: str = ""; detail: str = ""; icon: str = ""; price: int = 0; enabled: Optional[bool] = True
    system_prompt: str = ""; api_provider: str = ""; api_endpoint: str = ""; api_key_env: str = ""
    model_name: str = ""; api_type: str = "text"; request_format: str = ""; response_format: str = ""

class AdminAnnouncementRequest(BaseModel):
    title: str; content: str; is_active: Optional[bool] = True; is_important: Optional[bool] = False

class AdminUserCreditsRequest(BaseModel):
    delta: int; reason: str = ""

class AdminBanRequest(BaseModel):
    banned: bool; reason: str = ""

class AdminPasswordRequest(BaseModel):
    old_password: str; new_password: str

class AdminSettingsRequest(BaseModel):
    checkin_credits: Optional[int] = None; recharge_rate: Optional[int] = None; new_user_credits: Optional[int] = None
    weekly_free_quota: Optional[int] = None; chat_purchase_pack: Optional[int] = None

class AdminPaymentSettingsRequest(BaseModel):
    payment_alipay_qrcode: Optional[str] = None; payment_wechat_qrcode: Optional[str] = None
    payment_alipay_account: Optional[str] = None; payment_wechat_account: Optional[str] = None
    payment_instructions: Optional[str] = None

class PaymentRecordActionRequest(BaseModel):
    admin_note: str = ""

class RechargeTierRequest(BaseModel):
    name: str; amount: int; credits: int; bonus: int = 0; is_active: bool = True; sort_order: int = 0


# ═══════════════════════════════════════════════════════════════════════
# 用户路由
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/user/register")
async def register(req: RegisterRequest):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = %s", (req.email,))
        if cur.fetchone():
            return error_response("该邮箱已被注册")
        nc = get_setting_int("new_user_credits", 10)
        cur.execute("INSERT INTO users (username, email, password_hash, credits) VALUES (%s, %s, %s, %s) RETURNING id",
                     (req.username, req.email, bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode(), nc))
        user_id = cur.fetchone()["id"]
        cur.execute("INSERT INTO ai_configs (user_id) VALUES (%s)", (user_id,))
        # 初始化本周聊天额度
        _get_or_create_chat_usage(user_id, conn)
    return success_response({"token": create_token(user_id), "user_id": user_id, "username": req.username, "credits": nc}, "注册成功")


@app.post("/api/user/login")
async def login(req: LoginRequest):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = %s", (req.email,))
        user = cur.fetchone()
        if not user or not bcrypt.checkpw(req.password.encode(), user["password_hash"].encode() if isinstance(user["password_hash"], str) else user["password_hash"]):
            return error_response("邮箱或密码错误")
        if user["banned"]:
            return error_response(f"账号已被封禁: {user['banned_reason']}")
    return success_response({"token": create_token(user["id"]), "user_id": user["id"], "username": user["username"], "credits": user["credits"]}, "登录成功")


@app.get("/api/user/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, email, avatar, bio, credits, wechat_id, created_at, last_checkin, banned FROM users WHERE id = %s", (user["id"],))
        p = cur.fetchone()
    return success_response({
        "id": p["id"], "username": p["username"], "email": p["email"], "avatar": p["avatar"] or "",
        "bio": p["bio"] or "", "credits": p["credits"], "wechat_id": p["wechat_id"],
        "created_at": str(p["created_at"]), "last_checkin": str(p["last_checkin"]) if p["last_checkin"] else None, "banned": p["banned"],
    })


@app.put("/api/user/profile")
async def update_profile(req: UpdateProfileRequest, user: dict = Depends(get_current_user)):
    updates, params = [], []
    for f, v in [("username", req.username), ("avatar", req.avatar), ("bio", req.bio)]:
        if v is not None: updates.append(f"{f} = %s"); params.append(v)
    if not updates: return error_response("没有需要更新的字段")
    params.append(user["id"])
    with get_db() as conn:
        conn.cursor().execute(f"UPDATE users SET {', '.join(updates)} WHERE id = %s", params)
    return success_response(message="更新成功")


@app.post("/api/user/checkin")
async def daily_checkin(user: dict = Depends(get_current_user)):
    today = date.today()
    cc = get_setting_int("checkin_credits", 1)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT last_checkin, credits FROM users WHERE id = %s", (user["id"],))
        r = cur.fetchone()
        if r["last_checkin"] == today: return error_response("今天已经签到过了")
        nc = r["credits"] + cc
        cur.execute("UPDATE users SET credits = %s, last_checkin = %s WHERE id = %s", (nc, today, user["id"]))
    return success_response({"credits": nc, "checkin_date": str(today)}, f"签到成功，积分+{cc}")


# ═══════════════════════════════════════════════════════════════════════
# 聊天接口（含额度控制）
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/chat/quota")
async def get_chat_quota(user: dict = Depends(get_current_user)):
    """查询本周聊天额度"""
    with get_db() as conn:
        usage = _get_or_create_chat_usage(user["id"], conn)
    free_quota = get_setting_int("weekly_free_quota", 200)
    free_remaining = max(0, free_quota - usage["message_count"])
    purchased_remaining = usage["purchased_remaining"]
    return success_response({
        "weekly_free_quota": free_quota,
        "used_count": usage["message_count"],
        "free_remaining": free_remaining,
        "purchased_remaining": purchased_remaining,
        "total_remaining": free_remaining + purchased_remaining,
        "week_start": str(usage["week_start"]),
    })


@app.post("/api/chat/message")
async def chat_message(req: ChatMessageRequest, user: dict = Depends(get_current_user)):
    """发送消息给AI伙伴（网页端测试用）"""
    if not req.message.strip():
        return error_response("消息不能为空")
    # 检查额度
    ok, msg, quota_info = _check_and_consume_quota(user["id"])
    if not ok:
        return error_response(msg)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name, info, personality FROM ai_configs WHERE user_id = %s", (user["id"],))
        ai_config = cur.fetchone() or {"name": "玖喵", "info": "", "personality": "温柔可爱，善解人意"}
    result = _call_deepseek_chat(user["id"], req.message, dict(ai_config))
    return success_response({"reply": result["reply"], "tokens": result["tokens"], "quota": quota_info})


@app.post("/api/chat/purchase")
async def purchase_chat_quota(req: ChatPurchaseRequest, user: dict = Depends(get_current_user)):
    """购买额外聊天次数（1元=288条/包）"""
    if req.pack_count < 1:
        return error_response("至少购买1包")
    pack_size = get_setting_int("chat_purchase_pack", 288)
    total_count = req.pack_count * pack_size
    price_yuan = req.pack_count  # 1元/包
    price_cents = price_yuan * 100
    with get_db() as conn:
        cur = conn.cursor()
        usage = _get_or_create_chat_usage(user["id"], conn)
        cur.execute(
            "UPDATE chat_usage SET purchased_remaining = purchased_remaining + %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (total_count, usage["id"]),
        )
        cur.execute(
            "INSERT INTO chat_purchases (user_id, amount, price_cents) VALUES (%s, %s, %s)",
            (user["id"], total_count, price_cents),
        )
    return success_response({"purchased_count": total_count, "price_yuan": price_yuan}, f"购买成功，获得{total_count}条聊天次数")


# ═══════════════════════════════════════════════════════════════════════
# AI 配置路由
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/ai/config")
async def get_ai_config(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM ai_configs WHERE user_id = %s", (user["id"],))
        c = cur.fetchone()
        if not c:
            cur.execute("INSERT INTO ai_configs (user_id) VALUES (%s) RETURNING *", (user["id"],))
            c = cur.fetchone()
    return success_response({"id": c["id"], "name": c["name"], "info": c["info"], "personality": c["personality"], "updated_at": str(c["updated_at"])})


@app.put("/api/ai/config")
async def update_ai_config(req: AIConfigRequest, user: dict = Depends(get_current_user)):
    updates, params = [], []
    for f, v in [("name", req.name), ("info", req.info), ("personality", req.personality)]:
        if v is not None: updates.append(f"{f} = %s"); params.append(v)
    if not updates: return error_response("没有需要更新的字段")
    updates.append("updated_at = CURRENT_TIMESTAMP"); params.append(user["id"])
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM ai_configs WHERE user_id = %s", (user["id"],))
        if not cur.fetchone():
            cur.execute("INSERT INTO ai_configs (user_id, name, info, personality) VALUES (%s, %s, %s, %s)", (user["id"], req.name or "玖喵", req.info or "", req.personality or "温柔可爱，善解人意"))
        else:
            cur.execute(f"UPDATE ai_configs SET {', '.join(updates)} WHERE user_id = %s", params)
    return success_response(message="AI配置更新成功")


@app.get("/api/ai/memory")
async def get_ai_memory(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, role, content, created_at FROM chat_history WHERE user_id = %s ORDER BY created_at DESC LIMIT 50", (user["id"],))
    return success_response([{"id": r["id"], "role": r["role"], "content": r["content"], "created_at": str(r["created_at"])} for r in cur.fetchall()])


@app.get("/api/ai/diary")
async def get_ai_diary_list(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, date, content, mood, created_at FROM diary WHERE user_id = %s ORDER BY date DESC LIMIT 30", (user["id"],))
    return success_response([{"id": d["id"], "date": str(d["date"]), "content": d["content"], "mood": d["mood"], "created_at": str(d["created_at"])} for d in cur.fetchall()])


@app.get("/api/ai/diary/{diary_date}")
async def get_ai_diary(diary_date: str, user: dict = Depends(get_current_user)):
    try: target = datetime.strptime(diary_date, "%Y-%m-%d").date()
    except ValueError: return error_response("日期格式错误，请使用 YYYY-MM-DD")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM diary WHERE user_id = %s AND date = %s", (user["id"], target))
        d = cur.fetchone()
    if not d: return error_response("该日期没有日记")
    return success_response({"id": d["id"], "date": str(d["date"]), "content": d["content"], "mood": d["mood"], "created_at": str(d["created_at"])})


# ═══════════════════════════════════════════════════════════════════════
# 模块执行
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/module/{module_id}/execute")
async def execute_module(module_id: int, req: ModuleExecuteRequest, user: dict = Depends(get_current_user)):
    if not req.input.strip(): return error_response("输入内容不能为空")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM modules WHERE id = %s AND enabled = TRUE", (module_id,))
        module = cur.fetchone()
        if not module: return error_response("模块不存在或已下架")
        cur.execute("SELECT id FROM user_modules WHERE user_id = %s AND module_id = %s", (user["id"], module_id))
        if not cur.fetchone(): return error_response("您还未购买该模块")
    result = _execute_module_api(dict(module), req.input, user["id"])
    tokens = result.get("tokens", 0)
    with get_db() as conn:
        conn.cursor().execute("INSERT INTO api_usage (user_id, tokens_used, usage_type, module_id, endpoint) VALUES (%s, %s, %s, %s, %s)",
                              (user["id"], tokens, "module", module_id, module.get("api_endpoint", "")))
    return success_response({"result": result["result"], "tokens": tokens, "module_name": module["name"], "api_type": module.get("api_type", "text"), "extra": result.get("extra", {})})


# ═══════════════════════════════════════════════════════════════════════
# 微信相关路由
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/wechat/generate-binding-info")
async def generate_binding_info(user: dict = Depends(get_current_user)):
    bt = str(uuid.uuid4())
    bu = f"{WEBHOOK_URL}/api/wechat/bind-wechat?token={bt}" if WEBHOOK_URL else f"https://your-domain.com/api/wechat/bind-wechat?token={bt}"
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO wechat_connections (user_id, binding_token, status) VALUES (%s, %s, 'pending') ON CONFLICT (user_id) DO UPDATE SET binding_token = %s, status = 'pending'", (user["id"], bt, bt))
    return success_response({"binding_token": bt, "binding_url": bu, "instructions": "请在 OpenClaw 微信插件中复制以上绑定链接或 Token 完成绑定", "status": "pending"})


@app.post("/api/wechat/bind-wechat")
async def bind_wechat(req: WechatBindRequest, user: dict = Depends(get_current_user)):
    if not req.wechat_openid.strip(): return error_response("微信 OpenID 不能为空")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM wechat_connections WHERE wechat_openid = %s AND user_id != %s", (req.wechat_openid, user["id"]))
        if cur.fetchone(): return error_response("该微信号已被其他用户绑定")
        cur.execute("""INSERT INTO wechat_connections (user_id, wechat_openid, wechat_nickname, wechat_avatar, status, connected_at)
                       VALUES (%s, %s, %s, %s, 'connected', CURRENT_TIMESTAMP)
                       ON CONFLICT (user_id) DO UPDATE SET wechat_openid=%s, wechat_nickname=%s, wechat_avatar=%s, status='connected', connected_at=CURRENT_TIMESTAMP""",
                     (user["id"], req.wechat_openid, req.nickname, req.avatar, req.wechat_openid, req.nickname, req.avatar))
        cur.execute("UPDATE users SET wechat_id = %s WHERE id = %s", (req.wechat_openid, user["id"]))
    return success_response({"wechat_openid": req.wechat_openid, "nickname": req.nickname, "status": "connected"}, "微信绑定成功")


@app.post("/api/wechat/unbind-wechat")
async def unbind_wechat(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE wechat_connections SET wechat_openid=NULL, wechat_nickname='', wechat_avatar='', openclaw_id='', status='disconnected', connected_at=NULL WHERE user_id=%s", (user["id"],))
        if cur.rowcount == 0: return error_response("未找到绑定记录")
        cur.execute("UPDATE users SET wechat_id = NULL WHERE id = %s", (user["id"],))
    return success_response(message="微信解绑成功")


@app.get("/api/wechat/status")
async def get_wechat_status(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status, wechat_openid, wechat_nickname, wechat_avatar, openclaw_id, connected_at FROM wechat_connections WHERE user_id = %s", (user["id"],))
        r = cur.fetchone()
    if not r or r["status"] == "disconnected":
        return success_response({"status": "disconnected", "wechat_openid": None, "nickname": None})
    return success_response({"status": r["status"], "wechat_openid": r["wechat_openid"], "nickname": r["wechat_nickname"], "avatar": r["wechat_avatar"], "openclaw_id": r["openclaw_id"], "connected_at": str(r["connected_at"]) if r["connected_at"] else None})


@app.post("/api/wechat/message")
async def wechat_message_webhook(req: WechatMessageRequest):
    """Webhook：接收 OpenClaw 转发的微信消息（主要聊天入口）"""
    if not req.wechat_openid or not req.content:
        return error_response("缺少 wechat_openid 或 content")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM wechat_connections WHERE wechat_openid = %s AND status = 'connected'", (req.wechat_openid,))
        bind = cur.fetchone()
        if not bind: return error_response("该微信号未绑定任何用户")
        user_id = bind["user_id"]
        cur.execute("SELECT banned FROM users WHERE id = %s", (user_id,))
        u = cur.fetchone()
        if u and u["banned"]: return success_response({"reply": "您的账号已被封禁，无法使用此服务。"})

    # 检查聊天额度
    ok, msg, quota_info = _check_and_consume_quota(user_id)
    if not ok:
        return success_response({"reply": msg, "quota_exhausted": True})

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name, info, personality FROM ai_configs WHERE user_id = %s", (user_id,))
        ai_cfg = cur.fetchone() or {"name": "玖喵", "info": "", "personality": "温柔可爱，善解人意"}

    result = _call_deepseek_chat(user_id, req.content, dict(ai_cfg))
    # 在回复中附带剩余额度提示（如果剩余不多）
    remaining = quota_info.get("total_remaining", 0)
    reply = result["reply"]
    if remaining <= 10 and remaining > 0:
        reply += f"\n（本周剩余{remaining}条）"
    return success_response({"reply": reply})


# ═══════════════════════════════════════════════════════════════════════
# 支付系统
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/payment/info")
async def get_payment_info():
    """用户端获取收款方信息"""
    return success_response({
        "alipay_qrcode": get_setting("payment_alipay_qrcode", ""),
        "wechat_qrcode": get_setting("payment_wechat_qrcode", ""),
        "alipay_account": get_setting("payment_alipay_account", ""),
        "wechat_account": get_setting("payment_wechat_account", ""),
        "instructions": get_setting("payment_instructions", "请备注您的用户名"),
    })


@app.post("/api/payment/submit")
async def submit_payment(req: PaymentSubmitRequest, user: dict = Depends(get_current_user)):
    """用户提交支付截图，等待管理员确认"""
    if req.type not in ("credits", "chat_quota"):
        return error_response("type 只支持 'credits' 或 'chat_quota'")
    if req.amount_cents <= 0:
        return error_response("支付金额必须大于0")
    if req.quantity <= 0:
        return error_response("购买数量必须大于0")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO payment_records (user_id, type, amount_cents, quantity, payment_screenshot, status) VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id",
            (user["id"], req.type, req.amount_cents, req.quantity, req.payment_screenshot),
        )
        record_id = cur.fetchone()["id"]
    return success_response({"id": record_id, "status": "pending"}, "支付记录已提交，等待管理员确认")


@app.get("/api/payment/my-records")
async def get_my_payment_records(user: dict = Depends(get_current_user)):
    """查看自己的支付记录"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM payment_records WHERE user_id = %s ORDER BY created_at DESC LIMIT 20", (user["id"],))
        records = cur.fetchall()
    return success_response([{
        "id": r["id"], "type": r["type"], "amount_cents": r["amount_cents"],
        "quantity": r["quantity"], "status": r["status"], "admin_note": r["admin_note"] or "",
        "created_at": str(r["created_at"]), "confirmed_at": str(r["confirmed_at"]) if r["confirmed_at"] else None,
    } for r in records])


# ═══════════════════════════════════════════════════════════════════════
# 模块商店
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/modules")
async def get_modules():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM modules WHERE enabled = TRUE ORDER BY id")
    return success_response([{"id": m["id"], "name": m["name"], "description": m["description"], "detail": m.get("detail", ""), "model_name": m.get("model_name", ""), "price": m["price"], "icon": m["icon"], "api_type": m.get("api_type", "text")} for m in cur.fetchall()])


@app.get("/api/modules/{module_id}")
async def get_module_detail(module_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM modules WHERE id = %s", (module_id,))
        m = cur.fetchone()
    if not m: return error_response("模块不存在")
    return success_response({"id": m["id"], "name": m["name"], "description": m["description"], "detail": m.get("detail", ""), "model_name": m.get("model_name", ""), "price": m["price"], "icon": m["icon"], "enabled": m["enabled"], "api_type": m.get("api_type", "text")})


@app.post("/api/modules/purchase")
async def purchase_module(req: PurchaseModuleRequest, user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM modules WHERE id = %s AND enabled = TRUE", (req.module_id,))
        module = cur.fetchone()
        if not module: return error_response("模块不存在或已下架")
        cur.execute("SELECT id FROM user_modules WHERE user_id = %s AND module_id = %s", (user["id"], req.module_id))
        if cur.fetchone(): return error_response("已经购买过该模块")
        if user["credits"] < module["price"]: return error_response(f"积分不足，需要{module['price']}积分，当前{user['credits']}积分")
        cur.execute("UPDATE users SET credits = credits - %s WHERE id = %s", (module["price"], user["id"]))
        cur.execute("INSERT INTO user_modules (user_id, module_id) VALUES (%s, %s)", (user["id"], req.module_id))
    return success_response({"module_name": module["name"], "credits_spent": module["price"], "credits_remaining": user["credits"] - module["price"]}, "购买成功")


@app.get("/api/modules/my")
async def get_my_modules(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT m.*, um.purchased_at FROM modules m JOIN user_modules um ON m.id = um.module_id WHERE um.user_id = %s ORDER BY um.purchased_at DESC", (user["id"],))
    return success_response([{"id": m["id"], "name": m["name"], "description": m["description"], "detail": m.get("detail", ""), "model_name": m.get("model_name", ""), "price": m["price"], "icon": m["icon"], "api_type": m.get("api_type", "text"), "purchased_at": str(m["purchased_at"])} for m in cur.fetchall()])


# ═══════════════════════════════════════════════════════════════════════
# 积分
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/credits/balance")
async def get_credits_balance(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        r = conn.cursor().execute("SELECT credits FROM users WHERE id = %s", (user["id"],)) or None
        cur = conn.cursor()
        cur.execute("SELECT credits FROM users WHERE id = %s", (user["id"],))
        r = cur.fetchone()
    return success_response({"credits": r["credits"]})


@app.post("/api/credits/recharge")
async def recharge_credits(req: RechargeRequest, user: dict = Depends(get_current_user)):
    if req.amount <= 0: return error_response("充值金额必须大于0")
    rate = get_setting_int("recharge_rate", 5)
    ca = req.amount * rate
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET credits = credits + %s WHERE id = %s RETURNING credits", (ca, user["id"]))
        nc = cur.fetchone()["credits"]
    return success_response({"recharge_amount": req.amount, "credits_added": ca, "credits_total": nc}, f"充值成功，获得{ca}积分")


@app.get("/api/credits/tiers")
async def get_recharge_tiers():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM recharge_tiers WHERE is_active = TRUE ORDER BY sort_order")
    return success_response([{"id": t["id"], "name": t["name"], "amount": t["amount"], "credits": t["credits"], "bonus": t["bonus"], "sort_order": t["sort_order"]} for t in cur.fetchall()])


# ═══════════════════════════════════════════════════════════════════════
# 留言板
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/messages")
async def get_messages(board_type: str = "home", page: int = 1, page_size: int = 20):
    _validate_board_type(board_type)
    offset = (page - 1) * page_size
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM messages WHERE board_type = %s", (board_type,))
        total = cur.fetchone()["total"]
        cur.execute("SELECT m.*, u.username, u.avatar, (SELECT COUNT(*) FROM comments WHERE message_id = m.id) as comment_count FROM messages m JOIN users u ON m.user_id = u.id WHERE m.board_type = %s ORDER BY m.created_at DESC LIMIT %s OFFSET %s", (board_type, page_size, offset))
        msgs = cur.fetchall()
    return success_response({"total": total, "page": page, "page_size": page_size, "board_type": board_type, "messages": [{"id": m["id"], "content": m["content"], "board_type": m["board_type"], "username": m["username"], "avatar": m["avatar"] or "", "comment_count": m["comment_count"], "created_at": str(m["created_at"])} for m in msgs]})


@app.post("/api/messages")
async def post_message(req: PostMessageRequest, user: dict = Depends(get_current_user)):
    if not req.content.strip(): return error_response("留言内容不能为空")
    bt = _validate_board_type(req.board_type or "project1")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO messages (user_id, content, board_type) VALUES (%s, %s, %s) RETURNING id, created_at", (user["id"], req.content.strip(), bt))
        r = cur.fetchone()
    return success_response({"id": r["id"], "content": req.content.strip(), "board_type": bt, "created_at": str(r["created_at"])}, f"{_board_label(bt)}留言发表成功")


@app.get("/api/community/messages")
async def get_community_messages(page: int = 1, page_size: int = 20):
    return await get_messages(board_type="project1", page=page, page_size=page_size)


@app.post("/api/community/messages")
async def post_community_message(req: PostMessageRequest, user: dict = Depends(get_current_user)):
    req.board_type = "project1"; return await post_message(req, user)


@app.post("/api/community/messages/{message_id}/comment")
async def comment_message(message_id: int, req: CommentRequest, user: dict = Depends(get_current_user)):
    if not req.content.strip(): return error_response("评论内容不能为空")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, board_type FROM messages WHERE id = %s", (message_id,))
        msg = cur.fetchone()
        if not msg: return error_response("留言不存在")
        cur.execute("INSERT INTO comments (message_id, user_id, content) VALUES (%s, %s, %s) RETURNING id, created_at", (message_id, user["id"], req.content.strip()))
        r = cur.fetchone()
    return success_response({"id": r["id"], "message_id": message_id, "content": req.content.strip(), "board_type": msg["board_type"], "created_at": str(r["created_at"])})


@app.get("/api/community/moments")
async def get_moments(page: int = 1, page_size: int = 20):
    offset = (page - 1) * page_size
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT m.*, u.username, u.avatar FROM moments m JOIN users u ON m.user_id = u.id ORDER BY m.created_at DESC LIMIT %s OFFSET %s", (page_size, offset))
    return success_response([{"id": m["id"], "content": m["content"], "image_url": m["image_url"] or "", "username": m["username"], "avatar": m["avatar"] or "", "created_at": str(m["created_at"])} for m in cur.fetchall()])


@app.get("/api/community/announcements")
async def get_announcements():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM announcements WHERE is_active = TRUE ORDER BY is_important DESC, created_at DESC LIMIT 20")
    return success_response([{"id": a["id"], "title": a["title"], "content": a["content"], "is_important": a["is_important"], "created_at": str(a["created_at"])} for a in cur.fetchall()])


# ═══════════════════════════════════════════════════════════════════════
# 管理员路由
# ═══════════════════════════════════════════════════════════════════════

@app.post("/api/admin/login")
async def admin_login(req: LoginRequest):
    stored_pwd = get_setting("admin_password", ADMIN_PASSWORD)
    # 支持 bcrypt hash 验证和明文兼容（首次启动可能是明文环境变量）
    pwd_match = False
    try:
        pwd_match = bcrypt.checkpw(req.password.encode(), stored_pwd.encode() if isinstance(stored_pwd, str) else stored_pwd)
    except Exception:
        # 不是 hash 格式，按明文比较
        pwd_match = (req.password == stored_pwd)
    if not pwd_match:
        return error_response("管理员密码错误")
    # 如果是明文密码，自动升级为 bcrypt hash
    if not stored_pwd.startswith("$2"):
        set_setting("admin_password", bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode())
    return success_response({"token": create_token(0, is_admin=True), "is_admin": True}, "管理员登录成功")


# ─── 统计 ──────────────────────────────────────────────────────────────

@app.get("/api/admin/stats")
async def get_admin_stats(admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as t FROM users"); uc = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) as t FROM users WHERE created_at >= CURRENT_DATE"); tu = cur.fetchone()["t"]
        cur.execute("SELECT COALESCE(SUM(tokens_used),0) as t FROM api_usage WHERE usage_type='chat'"); ct = cur.fetchone()["t"]
        cur.execute("SELECT COALESCE(SUM(tokens_used),0) as t FROM api_usage WHERE usage_type='module'"); mt = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) as t FROM user_modules"); ms = cur.fetchone()["t"]
        cur.execute("SELECT COALESCE(SUM(m.price),0) as t FROM user_modules um JOIN modules m ON um.module_id=m.id"); rv = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) as t FROM announcements"); ac = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) as t FROM messages"); tm = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) as t FROM messages WHERE board_type='home'"); hm = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) as t FROM messages WHERE board_type='project1'"); cm = cur.fetchone()["t"]
        cur.execute("SELECT COUNT(*) as t FROM payment_records WHERE status='pending'"); pr = cur.fetchone()["t"]
    return success_response({"user_count": uc, "today_users": tu, "total_tokens": ct+mt, "chat_tokens": ct, "module_tokens": mt, "module_sales": ms, "revenue_credits": rv, "announcement_count": ac, "message_count": tm, "home_message_count": hm, "community_message_count": cm, "pending_payments": pr})


@app.get("/api/admin/stats/detailed")
async def get_admin_stats_detailed(admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT DATE(created_at) as d, usage_type, SUM(tokens_used) as tokens FROM api_usage WHERE created_at >= CURRENT_DATE - INTERVAL '30 days' GROUP BY d, usage_type ORDER BY d")
        tr = cur.fetchall()
        td = {}
        for r in tr:
            d = str(r["d"])
            if d not in td: td[d] = {"date": d, "chat_tokens": 0, "module_tokens": 0}
            td[d]["chat_tokens" if r["usage_type"] == "chat" else "module_tokens"] = r["tokens"]
        cur.execute("SELECT DATE(um.purchased_at) as d, SUM(m.price) as rev FROM user_modules um JOIN modules m ON um.module_id=m.id WHERE um.purchased_at >= CURRENT_DATE - INTERVAL '30 days' GROUP BY d ORDER BY d")
        rd = [{"date": str(r["d"]), "revenue": r["rev"]} for r in cur.fetchall()]
        cur.execute("SELECT m.id, m.name, m.price, COUNT(um.id) as sales, COALESCE(SUM(m.price),0) as revenue FROM modules m LEFT JOIN user_modules um ON m.id=um.module_id GROUP BY m.id ORDER BY sales DESC")
        mr = [{"id": r["id"], "name": r["name"], "price": r["price"], "sales": r["sales"], "revenue": r["revenue"] or 0} for r in cur.fetchall()]
        cur.execute("SELECT DATE(created_at) as d, COUNT(DISTINCT user_id) as au FROM chat_history WHERE created_at >= CURRENT_DATE - INTERVAL '7 days' GROUP BY d ORDER BY d")
        ad = [{"date": str(r["d"]), "active_users": r["au"]} for r in cur.fetchall()]
        cur.execute("SELECT DATE(created_at) as d, COUNT(*) as nu FROM users WHERE created_at >= CURRENT_DATE - INTERVAL '30 days' GROUP BY d ORDER BY d")
        nd = [{"date": str(r["d"]), "new_users": r["nu"]} for r in cur.fetchall()]
    return success_response({"token_daily": list(td.values()), "revenue_daily": rd, "module_ranking": mr, "active_daily": ad, "new_users_daily": nd})


# ─── 模块管理 ──────────────────────────────────────────────────────────

@app.get("/api/admin/modules")
async def admin_get_modules(admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT m.*, COALESCE(s.sales,0) as sales_count, COALESCE(s.revenue,0) as total_revenue FROM modules m LEFT JOIN (SELECT module_id, COUNT(*) as sales, SUM(price) as revenue FROM user_modules um JOIN modules md ON um.module_id=md.id GROUP BY module_id) s ON m.id=s.module_id ORDER BY m.id")
    return success_response([{"id": m["id"], "name": m["name"], "description": m["description"], "detail": m.get("detail",""), "icon": m["icon"], "price": m["price"], "enabled": m["enabled"], "system_prompt": m.get("system_prompt",""), "api_provider": m.get("api_provider",""), "api_endpoint": m.get("api_endpoint",""), "api_key_env": m.get("api_key_env",""), "model_name": m.get("model_name",""), "api_type": m.get("api_type","text"), "request_format": m.get("request_format",""), "response_format": m.get("response_format",""), "sales_count": m["sales_count"], "total_revenue": m["total_revenue"], "created_at": str(m["created_at"]), "updated_at": str(m["updated_at"])} for m in cur.fetchall()])


@app.post("/api/admin/modules")
async def admin_create_module(req: AdminModuleRequest, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO modules (name,description,detail,icon,price,enabled,system_prompt,api_provider,api_endpoint,api_key_env,model_name,api_type,request_format,response_format) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                     (req.name, req.description, req.detail, req.icon, req.price, req.enabled, req.system_prompt, req.api_provider, req.api_endpoint, req.api_key_env, req.model_name, req.api_type, req.request_format, req.response_format))
        mid = cur.fetchone()["id"]
    return success_response({"id": mid, "name": req.name}, "模块创建成功")


@app.put("/api/admin/modules/{module_id}")
async def admin_update_module(module_id: int, req: AdminModuleRequest, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM modules WHERE id = %s", (module_id,))
        if not cur.fetchone(): return error_response("模块不存在")
        cur.execute("UPDATE modules SET name=%s,description=%s,detail=%s,icon=%s,price=%s,enabled=%s,system_prompt=%s,api_provider=%s,api_endpoint=%s,api_key_env=%s,model_name=%s,api_type=%s,request_format=%s,response_format=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                     (req.name, req.description, req.detail, req.icon, req.price, req.enabled, req.system_prompt, req.api_provider, req.api_endpoint, req.api_key_env, req.model_name, req.api_type, req.request_format, req.response_format, module_id))
    return success_response({"id": module_id}, "模块更新成功")


@app.put("/api/admin/modules/{module_id}/toggle")
async def admin_toggle_module(module_id: int, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE modules SET enabled = NOT enabled, updated_at = CURRENT_TIMESTAMP WHERE id = %s RETURNING name, enabled", (module_id,))
        r = cur.fetchone()
    if not r: return error_response("模块不存在")
    return success_response({"id": module_id, "name": r["name"], "enabled": r["enabled"]}, f"模块{'上架' if r['enabled'] else '下架'}成功")


@app.delete("/api/admin/modules/{module_id}")
async def admin_delete_module(module_id: int, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM modules WHERE id = %s", (module_id,))
        m = cur.fetchone()
        if not m: return error_response("模块不存在")
        cur.execute("DELETE FROM modules WHERE id = %s", (module_id,))
    return success_response({"id": module_id, "name": m["name"]}, "模块删除成功")


# ─── 公告管理 ──────────────────────────────────────────────────────────

@app.get("/api/admin/announcements")
async def admin_get_announcements(admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM announcements ORDER BY created_at DESC")
    return success_response([{"id": a["id"], "title": a["title"], "content": a["content"], "is_active": a["is_active"], "is_important": a["is_important"], "created_at": str(a["created_at"]), "updated_at": str(a["updated_at"])} for a in cur.fetchall()])


@app.post("/api/admin/announcements")
async def admin_create_announcement(req: AdminAnnouncementRequest, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO announcements (title, content, is_active, is_important) VALUES (%s, %s, %s, %s) RETURNING id", (req.title, req.content, req.is_active, req.is_important))
        aid = cur.fetchone()["id"]
    return success_response({"id": aid, "title": req.title}, "公告创建成功")


@app.put("/api/admin/announcements/{aid}")
async def admin_update_announcement(aid: int, req: AdminAnnouncementRequest, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM announcements WHERE id = %s", (aid,))
        if not cur.fetchone(): return error_response("公告不存在")
        cur.execute("UPDATE announcements SET title=%s, content=%s, is_active=%s, is_important=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s", (req.title, req.content, req.is_active, req.is_important, aid))
    return success_response({"id": aid}, "公告更新成功")


@app.delete("/api/admin/announcements/{aid}")
async def admin_delete_announcement(aid: int, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT title FROM announcements WHERE id = %s", (aid,))
        a = cur.fetchone()
        if not a: return error_response("公告不存在")
        cur.execute("DELETE FROM announcements WHERE id = %s", (aid,))
    return success_response({"id": aid, "title": a["title"]}, "公告删除成功")


# ─── 用户管理 ──────────────────────────────────────────────────────────

@app.get("/api/admin/users")
async def admin_get_users(page: int = 1, page_size: int = 20, search: str = "", admin: dict = Depends(get_admin_user)):
    offset = (page - 1) * page_size
    with get_db() as conn:
        cur = conn.cursor()
        if search:
            cur.execute("SELECT COUNT(*) as t FROM users WHERE username ILIKE %s OR email ILIKE %s", (f"%{search}%", f"%{search}%"))
        else:
            cur.execute("SELECT COUNT(*) as t FROM users")
        total = cur.fetchone()["t"]
        if search:
            cur.execute("SELECT id,username,email,avatar,bio,credits,wechat_id,banned,banned_reason,created_at,last_checkin FROM users WHERE username ILIKE %s OR email ILIKE %s ORDER BY created_at DESC LIMIT %s OFFSET %s", (f"%{search}%", f"%{search}%", page_size, offset))
        else:
            cur.execute("SELECT id,username,email,avatar,bio,credits,wechat_id,banned,banned_reason,created_at,last_checkin FROM users ORDER BY created_at DESC LIMIT %s OFFSET %s", (page_size, offset))
        users = cur.fetchall()
    return success_response({"total": total, "page": page, "page_size": page_size, "users": [{"id": u["id"], "username": u["username"], "email": u["email"], "avatar": u["avatar"] or "", "bio": u["bio"] or "", "credits": u["credits"], "wechat_id": u["wechat_id"], "banned": u["banned"], "banned_reason": u["banned_reason"] or "", "created_at": str(u["created_at"]), "last_checkin": str(u["last_checkin"]) if u["last_checkin"] else None} for u in users]})


@app.get("/api/admin/users/{user_id}")
async def admin_get_user_detail(user_id: int, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        u = cur.fetchone()
        if not u: return error_response("用户不存在")
        cur.execute("SELECT * FROM ai_configs WHERE user_id = %s", (user_id,))
        ac = cur.fetchone()
        cur.execute("SELECT m.name, m.price, um.purchased_at FROM user_modules um JOIN modules m ON um.module_id=m.id WHERE um.user_id=%s ORDER BY um.purchased_at DESC", (user_id,))
        ps = cur.fetchall()
    return success_response({"user": {"id": u["id"], "username": u["username"], "email": u["email"], "avatar": u["avatar"] or "", "bio": u["bio"] or "", "credits": u["credits"], "wechat_id": u["wechat_id"], "banned": u["banned"], "banned_reason": u["banned_reason"] or "", "created_at": str(u["created_at"])}, "ai_config": {"name": ac["name"], "info": ac["info"], "personality": ac["personality"]} if ac else None, "purchases": [{"name": p["name"], "price": p["price"], "purchased_at": str(p["purchased_at"])} for p in ps]})


@app.put("/api/admin/users/{user_id}/ban")
async def admin_ban_user(user_id: int, req: AdminBanRequest, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
        u = cur.fetchone()
        if not u: return error_response("用户不存在")
        cur.execute("UPDATE users SET banned = %s, banned_reason = %s WHERE id = %s", (req.banned, req.reason if req.banned else "", user_id))
    return success_response({"id": user_id, "username": u["username"], "banned": req.banned}, f"用户{'封禁' if req.banned else '解封'}成功")


@app.put("/api/admin/users/{user_id}/credits")
async def admin_adjust_credits(user_id: int, req: AdminUserCreditsRequest, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT username, credits FROM users WHERE id = %s", (user_id,))
        u = cur.fetchone()
        if not u: return error_response("用户不存在")
        nc = u["credits"] + req.delta
        if nc < 0: return error_response("调整后积分不能为负数")
        cur.execute("UPDATE users SET credits = %s WHERE id = %s", (nc, user_id))
    return success_response({"id": user_id, "username": u["username"], "old_credits": u["credits"], "delta": req.delta, "new_credits": nc, "reason": req.reason}, "积分调整成功")


# ─── 留言管理 ──────────────────────────────────────────────────────────

@app.get("/api/admin/messages")
async def admin_get_messages(board_type: str = "all", page: int = 1, page_size: int = 20, admin: dict = Depends(get_admin_user)):
    offset = (page - 1) * page_size
    with get_db() as conn:
        cur = conn.cursor()
        if board_type == "all":
            cur.execute("SELECT COUNT(*) as t FROM messages"); total = cur.fetchone()["t"]
            cur.execute("SELECT m.*, u.username, u.avatar, (SELECT COUNT(*) FROM comments WHERE message_id = m.id) as comment_count FROM messages m JOIN users u ON m.user_id=u.id ORDER BY m.created_at DESC LIMIT %s OFFSET %s", (page_size, offset))
        else:
            cur.execute("SELECT COUNT(*) as t FROM messages WHERE board_type = %s", (board_type,)); total = cur.fetchone()["t"]
            cur.execute("SELECT m.*, u.username, u.avatar, (SELECT COUNT(*) FROM comments WHERE message_id = m.id) as comment_count FROM messages m JOIN users u ON m.user_id=u.id WHERE m.board_type = %s ORDER BY m.created_at DESC LIMIT %s OFFSET %s", (board_type, page_size, offset))
        msgs = cur.fetchall()
    return success_response({"total": total, "page": page, "page_size": page_size, "board_type": board_type, "messages": [{"id": m["id"], "content": m["content"], "board_type": m["board_type"], "user_id": m["user_id"], "username": m["username"], "comment_count": m["comment_count"], "created_at": str(m["created_at"])} for m in msgs]})


@app.delete("/api/admin/messages/{message_id}")
async def admin_delete_message(message_id: int, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM messages WHERE id = %s", (message_id,))
        if not cur.fetchone(): return error_response("留言不存在")
        cur.execute("DELETE FROM comments WHERE message_id = %s", (message_id,))
        cur.execute("DELETE FROM messages WHERE id = %s", (message_id,))
    return success_response({"id": message_id}, "留言删除成功")


# ─── 支付管理 ──────────────────────────────────────────────────────────

@app.get("/api/admin/payment/records")
async def admin_get_payment_records(status: str = "all", page: int = 1, page_size: int = 20, admin: dict = Depends(get_admin_user)):
    """管理员获取支付记录"""
    offset = (page - 1) * page_size
    with get_db() as conn:
        cur = conn.cursor()
        if status == "all":
            cur.execute("SELECT COUNT(*) as t FROM payment_records")
        else:
            cur.execute("SELECT COUNT(*) as t FROM payment_records WHERE status = %s", (status,))
        total = cur.fetchone()["t"]
        if status == "all":
            cur.execute("SELECT pr.*, u.username, u.email FROM payment_records pr JOIN users u ON pr.user_id=u.id ORDER BY pr.created_at DESC LIMIT %s OFFSET %s", (page_size, offset))
        else:
            cur.execute("SELECT pr.*, u.username, u.email FROM payment_records pr JOIN users u ON pr.user_id=u.id WHERE pr.status = %s ORDER BY pr.created_at DESC LIMIT %s OFFSET %s", (status, page_size, offset))
        records = cur.fetchall()
    return success_response({"total": total, "page": page, "page_size": page_size, "records": [{"id": r["id"], "user_id": r["user_id"], "username": r["username"], "email": r["email"], "type": r["type"], "amount_cents": r["amount_cents"], "quantity": r["quantity"], "payment_screenshot": r["payment_screenshot"] or "", "status": r["status"], "admin_note": r["admin_note"] or "", "created_at": str(r["created_at"]), "confirmed_at": str(r["confirmed_at"]) if r["confirmed_at"] else None} for r in records]})


@app.put("/api/admin/payment/records/{record_id}/confirm")
async def admin_confirm_payment(record_id: int, req: PaymentRecordActionRequest, admin: dict = Depends(get_admin_user)):
    """管理员确认支付到账"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM payment_records WHERE id = %s", (record_id,))
        record = cur.fetchone()
        if not record: return error_response("支付记录不存在")
        if record["status"] != "pending": return error_response("该记录已处理")
        # 确认并发放
        cur.execute("UPDATE payment_records SET status = 'confirmed', admin_note = %s, confirmed_at = CURRENT_TIMESTAMP WHERE id = %s", (req.admin_note, record_id))
        if record["type"] == "credits":
            cur.execute("UPDATE users SET credits = credits + %s WHERE id = %s", (record["quantity"], record["user_id"]))
        elif record["type"] == "chat_quota":
            usage = _get_or_create_chat_usage(record["user_id"], conn)
            cur.execute("UPDATE chat_usage SET purchased_remaining = purchased_remaining + %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (record["quantity"], usage["id"]))
    return success_response({"id": record_id, "type": record["type"], "quantity": record["quantity"]}, "支付确认成功，已发放")


@app.put("/api/admin/payment/records/{record_id}/reject")
async def admin_reject_payment(record_id: int, req: PaymentRecordActionRequest, admin: dict = Depends(get_admin_user)):
    """管理员拒绝支付"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM payment_records WHERE id = %s AND status = 'pending'", (record_id,))
        if not cur.fetchone(): return error_response("记录不存在或已处理")
        cur.execute("UPDATE payment_records SET status = 'rejected', admin_note = %s WHERE id = %s", (req.admin_note, record_id))
    return success_response({"id": record_id}, "已拒绝该支付")


# ─── 收款方设置 ────────────────────────────────────────────────────────

@app.get("/api/admin/settings/payment")
async def admin_get_payment_settings(admin: dict = Depends(get_admin_user)):
    return success_response({
        "payment_alipay_qrcode": get_setting("payment_alipay_qrcode", ""),
        "payment_wechat_qrcode": get_setting("payment_wechat_qrcode", ""),
        "payment_alipay_account": get_setting("payment_alipay_account", ""),
        "payment_wechat_account": get_setting("payment_wechat_account", ""),
        "payment_instructions": get_setting("payment_instructions", "请备注您的用户名"),
    })


@app.put("/api/admin/settings/payment")
async def admin_update_payment_settings(req: AdminPaymentSettingsRequest, admin: dict = Depends(get_admin_user)):
    if req.payment_alipay_qrcode is not None: set_setting("payment_alipay_qrcode", req.payment_alipay_qrcode)
    if req.payment_wechat_qrcode is not None: set_setting("payment_wechat_qrcode", req.payment_wechat_qrcode)
    if req.payment_alipay_account is not None: set_setting("payment_alipay_account", req.payment_alipay_account)
    if req.payment_wechat_account is not None: set_setting("payment_wechat_account", req.payment_wechat_account)
    if req.payment_instructions is not None: set_setting("payment_instructions", req.payment_instructions)
    return success_response(message="收款方设置更新成功")


# ─── 系统设置 ──────────────────────────────────────────────────────────

@app.get("/api/admin/settings")
async def admin_get_settings(admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value, description, updated_at FROM settings ORDER BY key")
        rows = cur.fetchall()
        cur.execute("SELECT * FROM recharge_tiers ORDER BY sort_order")
        tiers = cur.fetchall()
    sm = {r["key"]: {"value": r["value"], "description": r["description"], "updated_at": str(r["updated_at"])} for r in rows}
    return success_response({"settings": sm, "recharge_tiers": [{"id": t["id"], "name": t["name"], "amount": t["amount"], "credits": t["credits"], "bonus": t["bonus"], "is_active": t["is_active"], "sort_order": t["sort_order"]} for t in tiers]})


@app.put("/api/admin/settings")
async def admin_update_settings(req: AdminSettingsRequest, admin: dict = Depends(get_admin_user)):
    updates = {}
    for k, v in [("checkin_credits", req.checkin_credits), ("recharge_rate", req.recharge_rate), ("new_user_credits", req.new_user_credits), ("weekly_free_quota", req.weekly_free_quota), ("chat_purchase_pack", req.chat_purchase_pack)]:
        if v is not None: set_setting(k, str(v)); updates[k] = v
    if not updates: return error_response("没有需要更新的设置")
    return success_response(updates, "设置更新成功")


# ─── API Key 管理 ──────────────────────────────────────────────────────

class ApiKeysRequest(BaseModel):
    deepseek_api_key: str = ""
    imagine_api_key: str = ""


@app.get("/api/admin/settings/api-keys")
async def admin_get_api_keys(admin: dict = Depends(get_admin_user)):
    """获取 API Key 配置状态（不返回实际 key）"""
    deepseek_key = get_setting("deepseek_api_key", "")
    imagine_key = get_setting("imagine_api_key", "")
    return success_response({
        "deepseek_configured": bool(deepseek_key and deepseek_key != ""),
        "imagine_configured": bool(imagine_key and imagine_key != "")
    })


@app.post("/api/admin/settings/api-keys")
async def admin_set_api_keys(req: ApiKeysRequest, admin: dict = Depends(get_admin_user)):
    """保存 API Key（存储在数据库）"""
    if req.deepseek_api_key and req.deepseek_api_key != "••••••••••••••••":
        set_setting("deepseek_api_key", req.deepseek_api_key)
    if req.imagine_api_key and req.imagine_api_key != "••••••••••••••••":
        set_setting("imagine_api_key", req.imagine_api_key)
    return success_response(message="API Key 已保存")


@app.post("/api/admin/change-password")
async def admin_change_password(req: AdminPasswordRequest, admin: dict = Depends(get_admin_user)):
    """修改管理员密码（bcrypt hash 存储，修改后需重新登录）"""
    stored_pwd = get_setting("admin_password", ADMIN_PASSWORD)
    # 验证旧密码（兼容明文和 hash）
    pwd_match = False
    try:
        pwd_match = bcrypt.checkpw(req.old_password.encode(), stored_pwd.encode() if isinstance(stored_pwd, str) else stored_pwd)
    except Exception:
        pwd_match = (req.old_password == stored_pwd)
    if not pwd_match:
        return error_response("旧密码错误")
    if len(req.new_password) < 6:
        return error_response("新密码长度不能少于6位")
    # 用 bcrypt hash 存储新密码
    hashed = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
    set_setting("admin_password", hashed)
    return success_response(message="密码已修改，请重新登录")


# ─── 充值档位管理 ──────────────────────────────────────────────────────

@app.get("/api/admin/recharge-tiers")
async def admin_get_recharge_tiers(admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM recharge_tiers ORDER BY sort_order")
    return success_response([{"id": t["id"], "name": t["name"], "amount": t["amount"], "credits": t["credits"], "bonus": t["bonus"], "is_active": t["is_active"], "sort_order": t["sort_order"]} for t in cur.fetchall()])


@app.post("/api/admin/recharge-tiers")
async def admin_create_recharge_tier(req: RechargeTierRequest, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO recharge_tiers (name, amount, credits, bonus, is_active, sort_order) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id", (req.name, req.amount, req.credits, req.bonus, req.is_active, req.sort_order))
        tid = cur.fetchone()["id"]
    return success_response({"id": tid, "name": req.name}, "充值档位创建成功")


@app.put("/api/admin/recharge-tiers/{tier_id}")
async def admin_update_recharge_tier(tier_id: int, req: RechargeTierRequest, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM recharge_tiers WHERE id = %s", (tier_id,))
        if not cur.fetchone(): return error_response("充值档位不存在")
        cur.execute("UPDATE recharge_tiers SET name=%s, amount=%s, credits=%s, bonus=%s, is_active=%s, sort_order=%s WHERE id=%s", (req.name, req.amount, req.credits, req.bonus, req.is_active, req.sort_order, tier_id))
    return success_response({"id": tier_id}, "充值档位更新成功")


@app.delete("/api/admin/recharge-tiers/{tier_id}")
async def admin_delete_recharge_tier(tier_id: int, admin: dict = Depends(get_admin_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM recharge_tiers WHERE id = %s RETURNING name", (tier_id,))
        r = cur.fetchone()
    if not r: return error_response("充值档位不存在")
    return success_response({"id": tier_id}, "充值档位删除成功")


# ═══════════════════════════════════════════════════════════════════════
# 健康检查
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health_check():
    try:
        with get_db() as conn:
            conn.cursor().execute("SELECT 1")
        dbs = "connected"
    except Exception as e:
        dbs = f"error: {e}"
    return success_response({"status": "running", "database": dbs, "version": "2.3.0", "timestamp": datetime.now().isoformat()})


@app.get("/")
async def root():
    return success_response({"name": "哈基九-玖喵 WeChat API", "version": "2.3.0", "docs": "/docs"})
