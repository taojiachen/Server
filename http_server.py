import os
import json
import aiofiles
from aiohttp import web
from pathlib import Path
from datetime import datetime

from db_manager import AsyncMySQLManager
from esp_websocket_server import ESPWebSocketServer

PICTURE_ROOT = "picture"
PRESET_PHOTO_SUBDIR = "src"
PRESET_PHOTO_FILENAME = "user.png"

class HTTPServer:
    """手机端 API 服务器，提供三个核心接口"""
    
    def __init__(self, db_manager: AsyncMySQLManager, esp_server: ESPWebSocketServer):
        self.db_manager = db_manager
        self.esp_server = esp_server
        self.app = web.Application()
        self.runner = None
        self.site = None

        # 注册三个核心路由
        self.app.router.add_post('/api/milestone/config', self.set_milestone_config)
        self.app.router.add_get('/api/persona', self.get_persona)
        self.app.router.add_post('/api/preset_photo', self.upload_preset_photo)
        self.app.router.add_static('/picture', 'picture')  # 静态文件服务
        self.app.router.add_static('/task', 'task')        # 里程碑音频和图片静态目录

    # ==================== 1. 接收里程碑配置 ====================
    async def set_milestone_config(self, request):
        """
        POST /api/milestone/config
        支持两种JSON格式：
        1. 旧格式：{"device_mac": "xx", "milestones": [{"milestone_number":1,"question_count":3}]}
        2. 新格式：{"device_mac": "xx", "chapters": [{"chapter_id":1, "questions":[{"content":"..."}]}]}
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': '无效的 JSON'}, status=400)

        # 判断是新格式还是旧格式
        if 'chapters' in data:
            # ---------- 新格式：包含 chapters ----------
            device_mac = data.get('device_mac')
            if device_mac:
                device = await self.db_manager.get_device_by_mac(device_mac)
                if not device:
                    return web.json_response({'error': f'设备 {device_mac} 不存在'}, status=404)
                devices = [(device['id'], device_mac)]
            else:
                all_devs = await self.db_manager.get_all_devices()
                devices = [(d['id'], d['mac_address']) for d in all_devs]
                if not devices:
                    return web.json_response({'error': '数据库中没有设备'}, status=404)

            updated_count = 0
            for device_id, mac in devices:
                for chapter in data['chapters']:
                    milestone_num = chapter.get('chapter_id')
                    if milestone_num is None:
                        continue
                    questions_list = chapter.get('questions', [])
                    # 提取每个问题的 content 文本
                    question_texts = [q.get('content', '') for q in questions_list if q.get('content')]
                    if not question_texts:
                        continue
                    await self.db_manager.set_milestone_questions(device_id, milestone_num, question_texts)
                    updated_count += 1
            return web.json_response({'status': 'ok', 'updated': updated_count})

        else:
            # ---------- 旧格式：仅设置问题数量 ----------
            milestones = data.get('milestones')
            if not milestones or not isinstance(milestones, list):
                return web.json_response({'error': '缺少 milestones 字段或格式错误'}, status=400)

            device_mac = data.get('device_mac')
            if device_mac:
                device = await self.db_manager.get_device_by_mac(device_mac)
                if not device:
                    return web.json_response({'error': f'设备 {device_mac} 不存在'}, status=404)
                devices = [(device['id'], device_mac)]
            else:
                all_devs = await self.db_manager.get_all_devices()
                devices = [(d['id'], d['mac_address']) for d in all_devs]
                if not devices:
                    return web.json_response({'error': '数据库中没有设备'}, status=404)

            updated_count = 0
            for device_id, mac in devices:
                for m in milestones:
                    milestone_num = m.get('milestone_number')
                    q_count = m.get('question_count')
                    if milestone_num is None or q_count is None:
                        continue
                    await self.db_manager.set_milestone_question_count(device_id, milestone_num, q_count)
                    updated_count += 1
            return web.json_response({'status': 'ok', 'updated': updated_count})

    # ==================== 2. 返回所有儿童的里程碑任务完成信息 ====================
    async def get_persona(self, request):
        """
        GET /api/persona
        返回每个设备的每个里程碑详细数据：
        - milestone_number
        - total_questions: 问题列表（JSON数组）
        - answered_questions: 回答列表（JSON数组，顺序对应问题）
        - 人物画像分析: 该设备当前的人物画像（如果里程碑已完成则返回，否则null）
        - ai_photo_url: 里程碑图片路径
        - completed: 是否已完成
        """
        devices = await self.db_manager.get_all_devices()
        result = []

        for device in devices:
            mac = device['mac_address']
            safe_mac = mac.replace(':', '-')
            device_persona = device.get('persona_description')  # 设备整体人物画像

            # 获取该设备的所有里程碑配置（包含问题列表）
            milestones_rows = await self.db_manager.fetchall(
                "SELECT milestone_number, question_count, questions FROM milestones WHERE device_id = %s AND question_count > 0 ORDER BY milestone_number",
                (device['id'],)
            )

            milestones_info = []
            for row in milestones_rows:
                milestone_num = row['milestone_number']
                total_q = row['question_count']
                questions_json = row['questions']

                # 解析问题列表（JSON数组）
                questions_list = []
                if questions_json:
                    try:
                        questions_list = json.loads(questions_json)
                    except json.JSONDecodeError:
                        questions_list = []
                # 确保长度与 question_count 一致
                if len(questions_list) != total_q:
                    # 补全或截断
                    questions_list = (questions_list + [''] * total_q)[:total_q]

                # 获取该里程碑的回答（按 question_index 排序）
                answers_rows = await self.db_manager.fetchall(
                    "SELECT question_index, answer_text FROM milestone_answers WHERE device_id = %s AND milestone_number = %s ORDER BY question_index",
                    (device['id'], milestone_num)
                )
                # 构建回答数组（长度与问题数一致，缺失的回答用空字符串）
                answers_list = [""] * total_q
                for ans in answers_rows:
                    idx = ans['question_index'] - 1
                    if 0 <= idx < total_q:
                        answers_list[idx] = ans['answer_text'] or ""

                # 判断是否完成：所有问题都有非空回答
                completed = all(a != "" for a in answers_list)

                # 里程碑图片路径（无论是否完成都返回路径，前端可判断文件是否存在）
                milestone_photo_url = f"/task/milestones{milestone_num}/{safe_mac}/AI_picture/milestones{milestone_num}.png"

                # 人物画像分析：如果里程碑已完成且设备整体画像存在，则返回画像，否则 null
                persona_for_milestone = device_persona if (completed and device_persona) else None

                milestones_info.append({
                    "milestone_number": milestone_num,
                    "total_questions": questions_list,      # JSON数组
                    "answered_questions": answers_list,     # JSON数组
                    "人物画像分析": persona_for_milestone,
                    "ai_photo_url": milestone_photo_url,
                    "completed": completed
                })

            result.append({
                "mac": mac,
                "milestones": milestones_info
            })

        return web.json_response({
            "devices": result,
            "total_devices": len(result)
        })

    # ==================== 3. 接收预设照片 ====================
    async def upload_preset_photo(self, request):
        """
        POST /api/preset_photo
        multipart/form-data: fields: mac, photo (image file, PNG)
        图片保存到 picture/{safe_mac}/src/user.png
        """
        data = await request.post()
        mac = data.get('mac')
        photo_file = data.get('photo')
        if not mac or not photo_file:
            return web.json_response({'error': '缺少 mac 或 photo'}, status=400)

        # 验证设备存在
        device = await self.db_manager.get_device_by_mac(mac)
        if not device:
            return web.json_response({'error': '设备未注册'}, status=404)

        safe_mac = mac.replace(':', '-')
        save_dir = os.path.join(PICTURE_ROOT, safe_mac, PRESET_PHOTO_SUBDIR)
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, PRESET_PHOTO_FILENAME)

        content = await photo_file.read()
        async with aiofiles.open(save_path, 'wb') as f:
            await f.write(content)

        rel_path = os.path.join(PICTURE_ROOT, safe_mac, PRESET_PHOTO_SUBDIR, PRESET_PHOTO_FILENAME)
        return web.json_response({'status': 'ok', 'path': rel_path})

    # ==================== 服务器生命周期 ====================
    async def start(self, host='0.0.0.0', port=8443, ssl_context=None):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host, port, ssl_context=ssl_context)
        await self.site.start()
        protocol = "https" if ssl_context else "http"
        print(f"✅ HTTPS 服务器已启动: {protocol}://{host}:{port}")

    async def stop(self):
        if self.runner:
            await self.runner.shutdown()
            await self.runner.cleanup()