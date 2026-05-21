import os
import json
import asyncio
import aiofiles
from aiohttp import web, WSMsgType
from pathlib import Path
from datetime import datetime

from db_manager import AsyncMySQLManager
from esp_websocket_server import ESPWebSocketServer

PICTURE_ROOT = "picture"
PRESET_PHOTO_SUBDIR = "src"
PRESET_PHOTO_FILENAME = "user.png"

class HTTPServer:
    """手机端 API 服务器，提供 HTTPS 接口和 WebSocket 推送服务"""
    
    def __init__(self, db_manager: AsyncMySQLManager, esp_server: ESPWebSocketServer):
        self.db_manager = db_manager
        self.esp_server = esp_server
        self.app = web.Application()
        self.runner = None
        self.site = None
        self.subscribers = {}

        # 注册路由
        self.app.router.add_get('/api/persona', self.get_persona)
        self.app.router.add_post('/api/preset_photo', self.upload_preset_photo)
        self.app.router.add_get('/api/history', self.get_history)
        self.app.router.add_get('/api/alert/subscribe', self.subscribe_alerts)
        self.app.router.add_static('/picture', 'picture')

    # async def get_persona(self, request):
    #     """
    #     GET /api/persona?mac=XX:XX:XX:XX:XX:XX&title=自定义标题（可选）
    #     返回新格式的 JSON
    #     """
    #     mac = request.query.get('mac')
    #     if not mac:
    #         return web.json_response({'error': 'missing mac'}, status=400)

    #     device = await self.db_manager.get_device_by_mac(mac)
    #     if not device:
    #         return web.json_response({'error': 'device not found'}, status=404)

    #     persona = device.get('persona_description', '')
    #     ai_photo_url = device.get('ai_generated_photo_url', '')
        
    #     start_time = device.get('updated_at')
    #     if start_time is None:
    #         start_time = datetime.now()
    #     start_time_str = start_time.isoformat() if isinstance(start_time, datetime) else str(start_time)
        
    #     end_time_str = datetime.now().isoformat()
    #     title = request.query.get('title', '成长里程碑')
        
    #     response_data = {
    #         "title": title,
    #         "start_time": start_time_str,
    #         "end_time": end_time_str,
    #         "data": persona,
    #         "ai_photo_url": ai_photo_url
    #     }
    #     return web.json_response(response_data)

async def get_persona(self, request):
    """
    GET /api/persona?mac=XX:XX:XX:XX:XX:XX&title=自定义标题（可选）
    返回老师对学生的总体评价（完全硬编码）
    """
    # 完全硬编码，不依赖数据库
    persona = (
        "老师仔细看了你在蒜苗种植任务中的三个回答，心里特别高兴！"
        "你把一颗小小的蒜瓣照顾得那么好，还收获了这么多发现和感悟，"
        "老师要给你一个大大的“优”，再奖励你一颗🌟！\n\n"
        "从科学观察的角度看：你准确地找出了蒜苗生长需要的“水、阳光、空气、温度”，"
        "还能说出“根在水里，芽往阳光长”这样的细节，说明你有一双会观察的眼睛和一颗会思考的脑袋。"
        "你发现的“转杯子让蒜苗长直”这个办法，连老师都觉得特别妙！\n\n"
        "从动手实践的角度看：你总结的“水不能没过整个蒜瓣”“每天换水”“剥掉黏皮”等经验，"
        "都是实实在在从每天的照顾中得来的。你不仅勤快，还会总结方法，这比光看说明书厉害多了。"
        "你已经是一个合格的小园丁啦！\n\n"
        "从情感态度的角度看：老师最感动的是你说的“做事要耐心”“像爱护小宝宝一样爱护它们”“给妈妈炒菜”。"
        "你能从种蒜苗这件事里懂得坚持、责任和感恩，这比长高的蒜苗本身更宝贵。"
        "相信以后不管学什么、做什么，你都会像照顾蒜苗一样，有耐心、有爱心。\n\n"
        "一点小小的期待：等蒜苗完全成熟那天，老师很想尝尝你亲手种的蒜苗炒的菜。"
        "也希望你把这次的经验写进日记里，或者画一幅蒜苗长大的画，让美好的记忆留下来。\n\n"
        "继续保持这份好奇心和坚持，你一定会越来越棒！"
    )
    
    # 硬编码时间范围（或使用固定字符串）
    start_time_str = "2026-05-01T00:00:00"
    end_time_str = datetime.now().isoformat()  # 或者硬编码固定时间
    
    # 硬编码标题（可通过查询参数覆盖，或忽略参数）
    title = request.query.get('title', '蒜苗种植任务 · 老师总体评价')
    
    # 硬编码 AI 图片 URL（或留空）
    ai_photo_url = "picture/10-51-DB-84-C4-48/AI_Generat/user.png"
    
    response_data = {
        "title": title,
        "start_time": start_time_str,
        "end_time": end_time_str,
        "data": persona,
        "ai_photo_url": ai_photo_url
    }
    return web.json_response(response_data)

    async def upload_preset_photo(self, request):
        """
        POST /api/preset_photo
        multipart/form-data: fields: mac, photo (image file)
        """
        data = await request.post()
        mac = data.get('mac')
        photo_file = data.get('photo')
        if not mac or not photo_file:
            return web.json_response({'error': 'missing mac or photo'}, status=400)

        device = await self.db_manager.get_device_by_mac(mac)
        if not device:
            return web.json_response({'error': 'device not found'}, status=404)

        safe_mac = mac.replace(':', '-')
        device_dir = os.path.join(PICTURE_ROOT, safe_mac)
        src_dir = os.path.join(device_dir, PRESET_PHOTO_SUBDIR)
        os.makedirs(src_dir, exist_ok=True)
        save_path = os.path.join(src_dir, PRESET_PHOTO_FILENAME)

        content = await photo_file.read()
        async with aiofiles.open(save_path, 'wb') as f:
            await f.write(content)

        rel_path = os.path.join(PICTURE_ROOT, safe_mac, PRESET_PHOTO_SUBDIR, PRESET_PHOTO_FILENAME)
        await self.db_manager.update_preset_photo(mac, rel_path)

        return web.json_response({'status': 'ok', 'path': rel_path})

    async def get_history(self, request):
        """
        GET /api/history?mac=XX:XX:XX:XX:XX:XX
        返回对话历史，并将 created_at 转换为 ISO 字符串
        """
        mac = request.query.get('mac')
        if not mac:
            return web.json_response({'error': 'missing mac'}, status=400)

        device = await self.db_manager.get_device_by_mac(mac)
        if not device:
            return web.json_response({'error': 'device not found'}, status=404)

        sql = """
            SELECT m.role, m.content, m.created_at
            FROM messages m
            JOIN conversations c ON m.conversation_id = c.id
            JOIN devices d ON c.device_id = d.id
            WHERE d.mac_address = %s
            ORDER BY m.created_at ASC
        """
        rows = await self.db_manager.fetchall(sql, (mac,))
        
        for row in rows:
            if 'created_at' in row and isinstance(row['created_at'], datetime):
                row['created_at'] = row['created_at'].isoformat()
        
        return web.json_response(rows)

    async def subscribe_alerts(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        mac = request.query.get('mac')
        if not mac:
            await ws.close(code=1008, message=b'missing mac')
            return ws

        if mac not in self.subscribers:
            self.subscribers[mac] = []
        self.subscribers[mac].append(ws)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.CLOSE:
                    break
                if msg.data == 'ping':
                    await ws.send_str('pong')
        finally:
            if mac in self.subscribers:
                self.subscribers[mac].remove(ws)
                if not self.subscribers[mac]:
                    del self.subscribers[mac]
        return ws

    async def broadcast_alert(self, mac: str, alert_data: dict):
        if mac not in self.subscribers:
            return
        to_remove = []
        for ws in self.subscribers[mac]:
            try:
                await ws.send_json(alert_data)
            except Exception:
                to_remove.append(ws)
        for ws in to_remove:
            self.subscribers[mac].remove(ws)
        if not self.subscribers[mac]:
            del self.subscribers[mac]

    async def start(self, host='0.0.0.0', port=8443, ssl_context=None):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port, ssl_context=ssl_context)
        await self.site.start()
        protocol = "https" if ssl_context else "http"
        print(f"✅ {protocol.upper()} 服务器已启动 on {protocol}://{host}:{port}")

    async def stop(self):
        if self.runner:
            await self.runner.shutdown()
            await self.runner.cleanup()