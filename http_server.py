import os
import json
import asyncio
import aiofiles
from aiohttp import web, WSMsgType
from pathlib import Path
from datetime import datetime

from db_manager import AsyncMySQLManager
from esp_websocket_server import ESPWebSocketServer

# 图片存储根目录（与 AI_toy_picture.py 中的 PICTURE_ROOT 保持一致）
PICTURE_ROOT = "picture"
# 预设照片存放的子目录
PRESET_PHOTO_SUBDIR = "src"
# 预设照片的文件名固定为 user.png
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
        self.app.router.add_post('/api/emergency', self.emergency_command)
        self.app.router.add_get('/api/alert/subscribe', self.subscribe_alerts)
        self.app.router.add_static('/picture', 'picture')

    async def get_persona(self, request):
        """
        GET /api/persona?mac=XX:XX:XX:XX:XX:XX&title=自定义标题（可选）
        返回新格式的 JSON
        """
        mac = request.query.get('mac')
        if not mac:
            return web.json_response({'error': 'missing mac'}, status=400)

        device = await self.db_manager.get_device_by_mac(mac)
        if not device:
            return web.json_response({'error': 'device not found'}, status=404)

        persona = device.get('persona_description', '')
        ai_photo_url = device.get('ai_generated_photo_url', '')
        
        # 获取画像生成时间（updated_at）
        start_time = device.get('updated_at')
        if start_time is None:
            start_time = datetime.now()
        start_time_str = start_time.isoformat() if isinstance(start_time, datetime) else str(start_time)
        
        # 当前请求时间
        end_time_str = datetime.now().isoformat()
        
        # 标题（可从查询参数获取，默认“成长里程碑”）
        title = request.query.get('title', '成长里程碑')
        
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
        
        # 将 datetime 对象转换为 ISO 字符串
        for row in rows:
            if 'created_at' in row and isinstance(row['created_at'], datetime):
                row['created_at'] = row['created_at'].isoformat()
        
        return web.json_response(rows)

    async def emergency_command(self, request):
        """
        POST /api/emergency
        multipart/form-data: fields: mac, audio (audio/opus file)
        """
        data = await request.post()
        mac = data.get('mac')
        audio_file = data.get('audio')
        if not mac or not audio_file:
            return web.json_response({'error': 'missing mac or audio'}, status=400)

        device = await self.db_manager.get_device_by_mac(mac)
        if not device:
            return web.json_response({'error': 'device not found'}, status=404)

        ogg_opus_bytes = await audio_file.read()
        if len(ogg_opus_bytes) == 0:
            return web.json_response({'error': 'empty audio file'}, status=400)

        success = await self.esp_server.send_emergency_audio(mac, ogg_opus_bytes)
        if success:
            return web.json_response({'status': 'ok'})
        else:
            return web.json_response({'error': 'device not connected or send failed'}, status=503)

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