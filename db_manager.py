import aiomysql
import json
from typing import Dict, Any, Optional, List

class AsyncMySQLManager:
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
                id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                name VARCHAR(100) COMMENT '组名称',
                milestone_count INT NOT NULL DEFAULT 0 COMMENT '该组下里程碑总数（如3个里程碑）',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='分组/班级表';""",

            """CREATE TABLE IF NOT EXISTS devices (
                id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                mac_address VARCHAR(17) NOT NULL UNIQUE COMMENT '设备MAC地址，格式如F0:9E:9E:22:22:DC',
                group_id INT NOT NULL COMMENT '所属分组ID，关联groups.id',
                persona_description TEXT COMMENT '儿童人物画像（由LLM生成）',
                last_analysis_count INT DEFAULT 0 COMMENT '上次自动分析时的对话总条数，用于增量分析',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                FOREIGN KEY (group_id) REFERENCES `groups`(id) ON DELETE CASCADE,
                INDEX idx_group (group_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备表';""",

            """CREATE TABLE IF NOT EXISTS `milestones` (
                id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                device_id INT NOT NULL COMMENT '关联的设备ID',
                milestone_number INT NOT NULL COMMENT '里程碑序号（1,2,3...）',
                assessment_goal TEXT COMMENT '考核目标（整体描述）',
                assessment_evaluation TEXT COMMENT '考核评价（可后续补充）',
                question_count INT NOT NULL DEFAULT 0 COMMENT '该里程碑包含的问题数量',
                questions JSON NULL COMMENT '问题列表，JSON数组，如["问题1文本","问题2文本"]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
                UNIQUE KEY unique_device_milestone (device_id, milestone_number)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='里程碑配置表';""",

            """CREATE TABLE IF NOT EXISTS `milestone_answers` (
                id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                device_id INT NOT NULL COMMENT '关联的设备ID',
                milestone_number INT NOT NULL COMMENT '里程碑序号',
                question_index INT NOT NULL COMMENT '问题序号（从1开始）',
                answer_text TEXT COMMENT '语音识别得到的文字回答',
                answer_audio_path VARCHAR(500) COMMENT '回答音频文件的存储路径（相对路径）',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
                UNIQUE KEY unique_device_milestone_question (device_id, milestone_number, question_index)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='里程碑回答记录表';""",

            """CREATE TABLE IF NOT EXISTS conversations (
                id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                device_id INT NOT NULL COMMENT '关联的设备ID',
                role ENUM('user','AI','system') NOT NULL COMMENT '对话角色：user=儿童，AI=AI助手，system=系统',
                content TEXT NOT NULL COMMENT '对话文本内容',
                current_milestone INT NOT NULL DEFAULT 1 COMMENT '对话时的里程碑数（用于上下文标记）',
                summary_context TEXT COMMENT '对话上下文摘要（由LLM定期生成）',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE,
                INDEX idx_device_time (device_id, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='对话历史表';"""
        ]
        for stmt in statements:
            await self.execute(stmt)
        # 兼容旧表：尝试添加 questions 列（若已存在则忽略）
        try:
            await self.execute("ALTER TABLE milestones ADD COLUMN questions JSON NULL COMMENT '问题列表，JSON数组'")
        except Exception:
            pass
        # 为 devices 表添加 current_milestone 列（如果不存在）
        try:
            await self.execute("ALTER TABLE devices ADD COLUMN current_milestone INT NOT NULL DEFAULT 1 COMMENT '当前进行到的里程碑序号'")
            print("✅ 已为 devices 表添加 current_milestone 列")
        except Exception:
            pass
        # 确保现有设备的 current_milestone 有值
        await self.execute("UPDATE devices SET current_milestone = 1 WHERE current_milestone IS NULL")
        print("✅ 数据库表初始化完成")

    # ==================== 组操作 ====================
    async def create_group(self, name: str = None, milestone_count: int = 0) -> int:
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "INSERT INTO `groups` (name, milestone_count) VALUES (%s, %s)",
                    (name or '', milestone_count)
                )
                await conn.commit()
                return cur.lastrowid

    async def get_group_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return await self.fetchone("SELECT * FROM `groups` WHERE name = %s", (name,))

    async def get_default_group_id(self) -> int:
        row = await self.fetchone("SELECT id FROM `groups` WHERE name = %s", ('三年一班',))
        if not row:
            group_id = await self.create_group('三年一班', 3)
            print(f"✅ 已创建默认分组 '三年一班' (id={group_id})")
            return group_id
        return row['id']

    # ==================== 设备操作 ====================
    async def add_device(self, mac: str, group_id: int) -> int:
        existing = await self.get_device_by_mac(mac)
        if existing:
            return existing['id']
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

    async def upsert_device(self, mac: str, name: str = None) -> int:
        device = await self.get_device_by_mac(mac)
        if device:
            return device['id']
        group_id = await self.get_default_group_id()
        try:
            device_id = await self.add_device(mac, group_id)
            print(f"✅ 设备 {mac} 已添加到数据库，device_id={device_id}")
            return device_id
        except Exception as e:
            print(f"❌ 添加设备失败: {e}")
            raise

    async def get_device_current_milestone(self, mac: str) -> int:
        """获取设备当前所处的里程碑序号，默认为 1"""
        device = await self.get_device_by_mac(mac)
        if not device:
            return 1
        return device.get('current_milestone', 1)

    async def update_device_current_milestone(self, mac: str, milestone: int):
        """更新设备当前里程碑序号"""
        device = await self.get_device_by_mac(mac)
        if not device:
            return
        await self.execute(
            "UPDATE devices SET current_milestone = %s WHERE id = %s",
            (milestone, device['id'])
        )

    async def get_total_milestones_for_device(self, mac: str) -> int:
        """获取设备所属分组的总里程碑数"""
        device = await self.get_device_by_mac(mac)
        if not device:
            return 3
        group = await self.fetchone("SELECT milestone_count FROM `groups` WHERE id = %s", (device['group_id'],))
        return group['milestone_count'] if group else 3

    # ==================== 对话操作 ====================
    async def add_message(self, mac: str, role: str, content: str, current_milestone: int = 1):
        if role not in ('user', 'AI', 'system'):
            raise ValueError("角色必须为 user / AI / system")
        device = await self.get_device_by_mac(mac)
        if not device:
            print(f"⚠️ 设备 {mac} 不存在，尝试自动创建")
            await self.upsert_device(mac)
            device = await self.get_device_by_mac(mac)
            if not device:
                raise ValueError(f"设备 {mac} 创建失败")
        await self.execute(
            "INSERT INTO conversations (device_id, role, content, current_milestone) VALUES (%s, %s, %s, %s)",
            (device['id'], role, content, current_milestone)
        )

    async def get_recent_messages(self, mac: str, limit: int = 100) -> List[Dict[str, Any]]:
        device = await self.get_device_by_mac(mac)
        if not device:
            return []
        rows = await self.fetchall(
            """
            SELECT role, content, current_milestone, created_at
            FROM conversations
            WHERE device_id = %s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (device['id'], limit)
        )
        return rows
    
    async def get_messages_by_milestone(self, mac: str, milestone_number: int, limit: int = 500) -> List[Dict[str, Any]]:
        """获取指定设备在特定里程碑期间的对话记录（按时间正序）"""
        device = await self.get_device_by_mac(mac)
        if not device:
            return []
        rows = await self.fetchall(
            """
            SELECT role, content, current_milestone, created_at
            FROM conversations
            WHERE device_id = %s AND current_milestone = %s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (device['id'], milestone_number, limit)
        )
        return rows

    async def get_total_message_count(self, mac: str) -> int:
        device = await self.get_device_by_mac(mac)
        if not device:
            return 0
        result = await self.fetchone(
            "SELECT COUNT(*) as cnt FROM conversations WHERE device_id = %s",
            (device['id'],)
        )
        return result['cnt'] if result else 0

    async def get_last_analysis_count(self, mac: str) -> int:
        device = await self.get_device_by_mac(mac)
        if not device:
            return 0
        return device.get('last_analysis_count', 0)

    async def set_last_analysis_count(self, mac: str, count: int):
        device = await self.get_device_by_mac(mac)
        if not device:
            return
        await self.execute(
            "UPDATE devices SET last_analysis_count = %s WHERE id = %s",
            (count, device['id'])
        )

    async def get_latest_summary_by_mac(self, mac: str) -> Optional[str]:
        """获取设备最新会话的摘要"""
        device = await self.get_device_by_mac(mac)
        if not device:
            return None
        row = await self.fetchone(
            "SELECT summary_context FROM conversations WHERE device_id = %s ORDER BY created_at DESC LIMIT 1",
            (device['id'],)
        )
        return row['summary_context'] if row else None

    async def get_device_latest_conversation(self, mac: str) -> Optional[Dict[str, Any]]:
        """获取设备的最新会话记录（包含 id 和 summary_context）"""
        device = await self.get_device_by_mac(mac)
        if not device:
            return None
        return await self.fetchone(
            "SELECT id, summary_context FROM conversations WHERE device_id = %s ORDER BY created_at DESC LIMIT 1",
            (device['id'],)
        )

    async def create_conversation(self, mac: str) -> int:
        """为设备创建一条新的会话记录（初始角色为 system）"""
        device = await self.get_device_by_mac(mac)
        if not device:
            raise ValueError(f"设备 {mac} 不存在")
        await self.execute(
            "INSERT INTO conversations (device_id, role, content) VALUES (%s, 'system', '会话开始')",
            (device['id'],)
        )
        result = await self.fetchone("SELECT LAST_INSERT_ID() as id")
        return result['id']

    async def update_conversation_summary(self, conv_id: int, summary: str):
        """更新指定会话的摘要字段"""
        await self.execute(
            "UPDATE conversations SET summary_context = %s WHERE id = %s",
            (summary, conv_id)
        )

    # ==================== 任务统计（临时） ====================
    async def get_task_statistics(self, mac: str) -> Dict[str, Any]:
        return {
            "avg_health": 100,
            "avg_satiety": 100,
            "avg_cleanliness": 100,
            "health_on_time": 100,
            "satiety_on_time": 100,
            "cleanliness_on_time": 100,
            "record_count": 0
        }

    # ==================== 人物画像 ====================
    async def update_persona(self, mac: str, persona: str):
        device = await self.get_device_by_mac(mac)
        if not device:
            return
        await self.execute(
            "UPDATE devices SET persona_description = %s WHERE id = %s",
            (persona, device['id'])
        )

    # ==================== 里程碑相关（支持 questions JSON） ====================
    async def set_milestone_questions(self, device_id: int, milestone_number: int, questions: list):
        """存储某个里程碑的问题列表（JSON格式）"""
        questions_json = json.dumps(questions, ensure_ascii=False)
        question_count = len(questions)
        await self.execute(
            """
            INSERT INTO milestones (device_id, milestone_number, question_count, questions)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                question_count = VALUES(question_count),
                questions = VALUES(questions)
            """,
            (device_id, milestone_number, question_count, questions_json)
        )

    async def set_milestone_question_count(self, device_id: int, milestone_number: int, question_count: int):
        """仅设置问题数量（兼容旧逻辑）"""
        await self.execute(
            """
            INSERT INTO milestones (device_id, milestone_number, question_count)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE question_count = VALUES(question_count)
            """,
            (device_id, milestone_number, question_count)
        )

    async def get_milestone_answers(self, device_id: int, milestone_number: int) -> list:
        return await self.fetchall(
            "SELECT question_index, answer_text FROM milestone_answers WHERE device_id = %s AND milestone_number = %s ORDER BY question_index",
            (device_id, milestone_number)
        )

    async def get_all_milestone_qa_pairs(self, device_id: int) -> list:
        """获取设备所有已识别的问答对，从 questions JSON 中提取问题文本"""
        rows = await self.fetchall(
            """
            SELECT ma.milestone_number, ma.question_index, ma.answer_text, m.questions
            FROM milestone_answers ma
            JOIN milestones m ON m.device_id = ma.device_id AND m.milestone_number = ma.milestone_number
            WHERE ma.device_id = %s AND ma.answer_text IS NOT NULL AND ma.answer_text != ''
            ORDER BY ma.milestone_number, ma.question_index
            """,
            (device_id,)
        )
        qa_pairs = []
        for row in rows:
            milestone_num = row['milestone_number']
            q_index = row['question_index']
            questions_json = row['questions']
            if questions_json:
                try:
                    questions = json.loads(questions_json)
                    if isinstance(questions, list) and q_index <= len(questions):
                        question_text = questions[q_index - 1]
                    else:
                        question_text = f"里程碑{milestone_num}第{q_index}个问题"
                except:
                    question_text = f"里程碑{milestone_num}第{q_index}个问题"
            else:
                question_text = f"里程碑{milestone_num}第{q_index}个问题"
            qa_pairs.append({'question': question_text, 'answer': row['answer_text']})
        return qa_pairs

    async def get_milestone_qa_pairs_by_mac(self, mac: str) -> list:
        device = await self.get_device_by_mac(mac)
        if not device:
            return []
        rows = await self.fetchall(
            "SELECT assessment_goal AS question, child_answer_text AS answer FROM milestones WHERE device_id = %s AND assessment_goal IS NOT NULL AND child_answer_text IS NOT NULL AND assessment_goal != '' AND child_answer_text != '' ORDER BY milestone_number ASC",
            (device['id'],)
        )
        return rows

    # ==================== 所有设备信息 ====================
    async def get_all_devices(self) -> List[Dict[str, Any]]:
        return await self.fetchall("SELECT * FROM devices ORDER BY created_at DESC")

    # ==================== 兼容旧接口 ====================
    async def add_conversation_message(self, mac: str, role: str, content: str, current_milestone: int = None):
        if current_milestone is None:
            device = await self.get_device_by_mac(mac)
            if device:
                group_info = await self.fetchone("SELECT * FROM `groups` WHERE id = %s", (device['group_id'],))
                current_milestone = group_info.get('milestone_count', 1) if group_info else 1
            else:
                current_milestone = 1
        await self.add_message(mac, role, content, current_milestone)

    async def get_device_conversations(self, mac: str, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        return await self.get_recent_messages(mac, limit)

    async def get_recent_messages_by_device(self, mac: str, limit: int = 100) -> List[Dict[str, Any]]:
        return await self.get_recent_messages(mac, limit)