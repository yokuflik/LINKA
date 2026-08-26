import os
from dotenv import load_dotenv

# 1. טוען את משתני הסביבה מקובץ ה-.env לפני כל ייבוא אחר
load_dotenv()

import asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import select, text

# 2. מייבא את השם המדויק כפי שהוא מופיע בקובץ שלך
from database.connection import AsyncSessionLocal
from database.models.user import User
from database.models.chat import Chat
from database.models import chat, message, participant, private_chat_pair, user

admin_app = FastAPI(title="Linka Admin Dashboard")

@admin_app.get("/", response_class=HTMLResponse)
async def dashboard():
    # 3. משתמש בשם הנכון לפתיחת הסשן
    async with AsyncSessionLocal() as session:
        # 1. Fetch total user count
        users_count_result = await session.execute(select(text("COUNT(*)")).select_from(User))
        users_count = users_count_result.scalar()
admin_app = FastAPI(title="Linka Admin Dashboard")

@admin_app.get("/", response_class=HTMLResponse)
async def dashboard():
    """
    Fetches raw data directly from the DB and renders a simple HTML string.
    Bypasses all normal app authentication and REST routes.
    """
    async with AsyncSessionLocal() as session:
        # 1. Fetch total user count
        users_count_result = await session.execute(select(text("COUNT(*)")).select_from(User))
        users_count = users_count_result.scalar()

        # 2. Fetch the 10 most recent users for debugging
        recent_users_result = await session.execute(
            select(User).order_by(User.created_at.desc()).limit(10)
        )
        recent_users = recent_users_result.scalars().all()

        # 3. Fetch total chats count
        chats_count_result = await session.execute(select(text("COUNT(*)")).select_from(Chat))
        chats_count = chats_count_result.scalar()

    # Build the HTML string directly
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <title>Admin Debug</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 20px; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
        </style>
    </head>
    <body>
        <h1>System Debug Dashboard</h1>
        
        <h2>General Stats</h2>
        <ul>
            <li><strong>Total Users:</strong> {users_count}</li>
            <li><strong>Total Chats:</strong> {chats_count}</li>
        </ul>

        <h2>Recent Users</h2>
        <table>
            <tr>
                <th>ID (Snowflake)</th>
                <th>Phone Number</th>
                <th>Display Name</th>
                <th>Created At</th>
            </tr>
    """
    
    for user in recent_users:
        html_content += f"""
            <tr>
                <td>{user.id}</td>
                <td>{user.phone_number}</td>
                <td>{user.display_name or 'N/A'}</td>
                <td>{user.created_at}</td>
            </tr>
        """

    html_content += """
        </table>
    </body>
    </html>
    """
    return html_content

if __name__ == "__main__":
    # Runs a completely separate server on port 9000
    uvicorn.run(admin_app, host="127.0.0.1", port=9000)