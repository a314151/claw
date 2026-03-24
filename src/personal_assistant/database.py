import json
import sqlite3
from pathlib import Path

DB_PATH = Path("chat_history.db")

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
        """
    )
    return conn

def save_message(session_id: str, role: str, content: str | dict | list):
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False)
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content)
        )

def get_history(session_id: str) -> list[dict]:
    with _get_conn() as conn:
        cursor = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC",
            (session_id,)
        )
        rows = cursor.fetchall()
    
    history = []
    for role, content in rows:
        try:
            parsed_content = json.loads(content)
            if isinstance(parsed_content, (dict, list)):
                content = parsed_content
        except (json.JSONDecodeError, TypeError):
            pass
        history.append({"role": role, "content": content})
    return history
