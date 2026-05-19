import aiomysql
from typing import Dict, Any, Optional, List


class AsyncMySQLManager:
    """异步MySQL数据库管理器，提供连接池和单片机设备组管理操作"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.pool = None

    async def ensure_database(self):
        conn = await aiomysql.connect(
            host=self.config["host"],
            port=self.config["port"],
            user=self.config["user"],
            password=self.config["password"],
            charset=self.config["charset"],
            autocommit=True
        )
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{self.config['db']}` "
                    f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
                )
            print(f"✅ 数据库 `{self.config['db']}` 已确保存在")
        finally:
            conn.close()

    async def create_pool(self):
        self.pool = await aiomysql.create_pool(
            host=self.config["host"],
            port=self.config["port"],
            user=self.config["user"],
            password=self.config["password"],
            db=self.config["db"],
            minsize=self.config["minsize"],
            maxsize=self.config["maxsize"],
            charset=self.config["charset"],
            autocommit=True
        )
        print("✅ MySQL 连接池已创建")

    async def close_pool(self):
        if self.pool:
            self.pool.close()
            await self.pool.wait_closed()
            print("🔴 MySQL 连接池已关闭")

    async def execute(self, sql: str, args: tuple = None) -> int:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                affected = await cur.execute(sql, args)
                return affected

    async def fetchone(self, sql: str, args: tuple = None) -> Optional[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, args)
                return await cur.fetchone()

    async def fetchall(self, sql: str, args: tuple = None) -> List[Dict[str, Any]]:
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, args)
                return await cur.fetchall()

    # ==================== 建表 ====================
    async def init_tables(self):
        statements = [
            """CREATE TABLE IF NOT EXISTS `groups` (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) COMMENT '组名称',
                milestone_count INT NOT NULL DEFAULT 0 COMMENT '里程碑总数',
                current_milestone INT NOT NULL DEFAULT 1 COMMENT '当前所处里程碑数',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",

            """CREATE TABLE IF NOT EXISTS devices (
                id INT AUTO_INCREMENT PRIMARY KEY,
                mac_address VARCHAR(17) NOT NULL UNIQUE COMMENT 'MAC地址',
                group_id INT NOT NULL,
                conversation_summary TEXT COMMENT '对话上下文摘要',
                last_summary_message_count INT DEFAULT 0 COMMENT '上次生成摘要时的对话总数',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                FOREIGN KEY (group_id) REFERENCES `groups`(id) ON DELETE CASCADE,
                INDEX idx_group (group_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",

            """CREATE TABLE IF NOT EXISTS milestones (
                id INT AUTO_INCREMENT PRIMARY KEY,
                device_id INT NOT NULL COMMENT '关联设备',
                milestone_number INT NOT NULL COMMENT '里程碑序号',
                assessment_goal TEXT COMMENT '考核目标',
                assessment_evaluation TEXT COMMENT '考核评价',
                task_completion_image_url VARCHAR(500) COMMENT '任务完成验收图片URL',
                assessment_audio_url VARCHAR(500) COMMENT '考核指标音频URL',
                child_learning_ai_drawing_url VARCHAR(500) COMMENT '儿童学习成果AI绘图URL',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
                UNIQUE KEY unique_device_milestone (device_id, milestone_number)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",

            """CREATE TABLE IF NOT EXISTS conversations (
                id INT AUTO_INCREMENT PRIMARY KEY,
                device_id INT NOT NULL,
                role ENUM('user','AI','system') NOT NULL COMMENT '对话角色',
                content TEXT NOT NULL COMMENT '对话文本',
                current_milestone INT NOT NULL DEFAULT 1 COMMENT '对话时的里程碑数',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
                INDEX idx_device_time (device_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"""
        ]
        for stmt in statements:
            await self.execute(stmt)
        print("✅ 数据库表初始化完成（groups, devices, milestones, conversations）")

    # ==================== 组操作 ====================
    async def create_group(self, name: str = None, milestone_count: int = 0) -> int:
        """创建新组，返回组ID"""
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "INSERT INTO `groups` (name, milestone_count) VALUES (%s, %s)",
                    (name or '', milestone_count)
                )
                await conn.commit()
                return cur.lastrowid

    async def update_group_milestone(self, group_id: int, current_milestone: int):
        """更新组的当前里程碑"""
        await self.execute(
            "UPDATE `groups` SET current_milestone = %s WHERE id = %s",
            (current_milestone, group_id)
        )

    async def update_group_milestone_count(self, group_id: int, milestone_count: int):
        """更新组的里程碑总数"""
        await self.execute(
            "UPDATE `groups` SET milestone_count = %s WHERE id = %s",
            (milestone_count, group_id)
        )

    async def get_group_info(self, group_id: int) -> Optional[Dict[str, Any]]:
        """获取组信息"""
        return await self.fetchone("SELECT * FROM `groups` WHERE id = %s", (group_id,))

    async def get_group_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return await self.fetchone("SELECT * FROM `groups` WHERE name = %s", (name,))

    # ==================== 设备操作 ====================
    async def add_device(self, mac: str, group_id: int) -> int:
        """添加设备到指定组"""
        existing = await self.get_device_by_mac(mac)
        if existing:
            raise ValueError(f"设备 {mac} 已存在")
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "INSERT INTO devices (mac_address, group_id) VALUES (%s, %s)",
                    (mac, group_id)
                )
                await conn.commit()
                return cur.lastrowid

    async def get_device_by_mac(self, mac: str) -> Optional[Dict[str, Any]]:
        return await self.fetchone("SELECT * FROM devices WHERE mac_address = %s", (mac,))

    async def get_devices_by_group(self, group_id: int) -> List[Dict[str, Any]]:
        """获取组内所有设备"""
        return await self.fetchall(
            "SELECT * FROM devices WHERE group_id = %s ORDER BY created_at", (group_id,)
        )

    async def get_device_group_info(self, mac: str) -> Optional[Dict[str, Any]]:
        """获取设备所属组的完整信息（包含里程碑数据）"""
        sql = """
            SELECT g.*, d.mac_address, d.id as device_id
            FROM devices d
            JOIN `groups` g ON d.group_id = g.id
            WHERE d.mac_address = %s
        """
        return await self.fetchone(sql, (mac,))

    # ==================== 对话摘要相关 ====================
    async def get_device_message_count(self, mac: str) -> int:
        """获取设备的对话总条数"""
        sql = """
            SELECT COUNT(*) as cnt FROM conversations c
            JOIN devices d ON c.device_id = d.id
            WHERE d.mac_address = %s
        """
        result = await self.fetchone(sql, (mac,))
        return result['cnt'] if result else 0

    async def get_messages_since_last_summary(self, mac: str) -> List[Dict[str, Any]]:
        """获取自上次摘要后新增的对话记录（用于生成新摘要）"""
        device = await self.get_device_by_mac(mac)
        if not device:
            raise ValueError(f"设备 {mac} 不存在")
        last_count = device.get('last_summary_message_count', 0)
        sql = """
            SELECT c.role, c.content, c.created_at
            FROM conversations c
            JOIN devices d ON c.device_id = d.id
            WHERE d.mac_address = %s
            ORDER BY c.created_at ASC
            LIMIT 100000 OFFSET %s
        """
        return await self.fetchall(sql, (mac, last_count))

    async def update_conversation_summary(self, mac: str, summary: str):
        """更新设备的对话摘要，并记录当前对话总数作为下次摘要的起点"""
        total = await self.get_device_message_count(mac)
        await self.execute(
            "UPDATE devices SET conversation_summary = %s, last_summary_message_count = %s WHERE mac_address = %s",
            (summary, total, mac)
        )

    async def get_conversation_summary(self, mac: str) -> Optional[str]:
        """获取设备当前的对话摘要"""
        device = await self.get_device_by_mac(mac)
        return device['conversation_summary'] if device else None

    async def needs_summary(self, mac: str, threshold: int = 50) -> bool:
        """检查设备对话数是否达到摘要阈值（每次新增50条触发）"""
        device = await self.get_device_by_mac(mac)
        if not device:
            return False
        total = await self.get_device_message_count(mac)
        last_count = device.get('last_summary_message_count', 0)
        return (total - last_count) >= threshold

    # ==================== 里程碑操作 ====================
    async def upsert_milestone(self, mac: str, milestone_number: int,
                               assessment_goal: str = None,
                               assessment_evaluation: str = None,
                               task_completion_image_url: str = None,
                               assessment_audio_url: str = None,
                               child_learning_ai_drawing_url: str = None):
        """创建或更新某个设备的某个里程碑记录"""
        device = await self.get_device_by_mac(mac)
        if not device:
            raise ValueError(f"设备 {mac} 不存在")

        existing = await self.fetchone(
            "SELECT id FROM milestones WHERE device_id = %s AND milestone_number = %s",
            (device["id"], milestone_number)
        )
        if existing:
            updates = []
            values = []
            if assessment_goal is not None:
                updates.append("assessment_goal = %s")
                values.append(assessment_goal)
            if assessment_evaluation is not None:
                updates.append("assessment_evaluation = %s")
                values.append(assessment_evaluation)
            if task_completion_image_url is not None:
                updates.append("task_completion_image_url = %s")
                values.append(task_completion_image_url)
            if assessment_audio_url is not None:
                updates.append("assessment_audio_url = %s")
                values.append(assessment_audio_url)
            if child_learning_ai_drawing_url is not None:
                updates.append("child_learning_ai_drawing_url = %s")
                values.append(child_learning_ai_drawing_url)
            if updates:
                sql = f"UPDATE milestones SET {', '.join(updates)} WHERE device_id = %s AND milestone_number = %s"
                values.extend([device["id"], milestone_number])
                await self.execute(sql, tuple(values))
        else:
            await self.execute(
                """INSERT INTO milestones 
                (device_id, milestone_number, assessment_goal, assessment_evaluation,
                 task_completion_image_url, assessment_audio_url, child_learning_ai_drawing_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (device["id"], milestone_number,
                 assessment_goal or '', assessment_evaluation or '',
                 task_completion_image_url or '', assessment_audio_url or '',
                 child_learning_ai_drawing_url or '')
            )

    async def get_milestone(self, mac: str, milestone_number: int) -> Optional[Dict[str, Any]]:
        """获取设备指定里程碑数据"""
        sql = """
            SELECT m.* FROM milestones m
            JOIN devices d ON m.device_id = d.id
            WHERE d.mac_address = %s AND m.milestone_number = %s
        """
        return await self.fetchone(sql, (mac, milestone_number))

    async def get_all_milestones_for_device(self, mac: str) -> List[Dict[str, Any]]:
        """获取设备所有里程碑数据"""
        sql = """
            SELECT m.* FROM milestones m
            JOIN devices d ON m.device_id = d.id
            WHERE d.mac_address = %s
            ORDER BY m.milestone_number ASC
        """
        return await self.fetchall(sql, (mac,))

    # ==================== 对话操作 ====================
    async def add_conversation_message(self, mac: str, role: str, content: str,
                                       current_milestone: int = None):
        """添加对话记录，自动获取设备当前里程碑数（若未提供）"""
        if role not in ('user', 'AI', 'system'):
            raise ValueError("角色必须为 user / AI / system")

        device = await self.get_device_by_mac(mac)
        if not device:
            raise ValueError(f"设备 {mac} 不存在")

        if current_milestone is None:
            group_info = await self.get_group_info(device["group_id"])
            current_milestone = group_info["current_milestone"] if group_info else 1

        await self.execute(
            "INSERT INTO conversations (device_id, role, content, current_milestone) VALUES (%s, %s, %s, %s)",
            (device["id"], role, content, current_milestone)
        )

    async def get_device_conversations(self, mac: str, limit: int = 100,
                                       offset: int = 0) -> List[Dict[str, Any]]:
        """获取设备的对话历史"""
        sql = """
            SELECT c.role, c.content, c.current_milestone, c.created_at
            FROM conversations c
            JOIN devices d ON c.device_id = d.id
            WHERE d.mac_address = %s
            ORDER BY c.created_at ASC
            LIMIT %s OFFSET %s
        """
        return await self.fetchall(sql, (mac, limit, offset))

    async def get_recent_conversations(self, mac: str, limit: int = 20) -> List[Dict[str, Any]]:
        """获取最近的对话记录（倒序）"""
        sql = """
            SELECT c.role, c.content, c.current_milestone, c.created_at
            FROM conversations c
            JOIN devices d ON c.device_id = d.id
            WHERE d.mac_address = %s
            ORDER BY c.created_at DESC
            LIMIT %s
        """
        rows = await self.fetchall(sql, (mac, limit))
        rows.reverse()
        return rows