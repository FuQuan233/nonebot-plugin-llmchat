"""
数据迁移脚本
将聊天数据从 JSON 文件迁移到数据库
"""
import asyncio
import json
import os
from collections import deque
from datetime import datetime

from nonebot import logger

from .config import Config, get_plugin_config
from .db_manager import DatabaseManager
from .models import ChatHistory, GroupChatState, PrivateChatState

# 获取插件数据目录
try:
    import nonebot_plugin_localstore as store
    data_dir = store.get_plugin_data_dir()
    data_file = store.get_plugin_data_file("llmchat_state.json")
    private_data_file = store.get_plugin_data_file("llmchat_private_state.json")
except ImportError:
    logger.warning("无法找到 nonebot_plugin_localstore，迁移可能失败")
    data_dir = None
    data_file = None
    private_data_file = None


async def migrate_from_json_to_db(plugin_config: Config):
    """从 JSON 文件迁移到数据库"""
    logger.info("开始从 JSON 文件迁移到数据库")
    
    if not data_file or not os.path.exists(data_file):
        logger.info("未找到群组状态 JSON 文件，跳过迁移")
        return
    
    try:
        # 迁移群组状态
        logger.info(f"正在迁移群组状态数据: {data_file}")
        with open(data_file, "r", encoding="utf8") as f:
            data = json.load(f)
        
        migrated_groups = 0
        for gid_str, state_data in data.items():
            try:
                gid = int(gid_str)
                # 检查是否已存在
                existing = await GroupChatState.get_or_none(group_id=gid)
                if existing:
                    logger.debug(f"群组 {gid} 已存在于数据库，跳过迁移")
                    continue
                
                # 创建新的状态记录
                await GroupChatState.create(
                    group_id=gid,
                    preset_name=state_data.get("preset", "off"),
                    group_prompt=state_data.get("group_prompt"),
                    output_reasoning_content=state_data.get("output_reasoning_content", False),
                    random_trigger_prob=state_data.get("random_trigger_prob", 0.05),
                    last_active=datetime.fromtimestamp(state_data.get("last_active", datetime.now().timestamp())),
                )
                
                # 创建历史记录
                messages = state_data.get("history", [])
                if messages:
                    await ChatHistory.create(
                        group_id=gid,
                        is_private=False,
                        messages_json=ChatHistory.serialize_messages(messages),
                    )
                
                migrated_groups += 1
                logger.debug(f"已迁移群组 {gid}（{len(messages)} 条消息）")
            
            except Exception as e:
                logger.error(f"迁移群组 {gid_str} 失败: {e}")
        
        logger.info(f"成功迁移 {migrated_groups} 个群组的状态")
    
    except Exception as e:
        logger.error(f"迁移群组状态失败: {e}")
    
    # 迁移私聊状态
    if plugin_config.llmchat.enable_private_chat and private_data_file and os.path.exists(private_data_file):
        try:
            logger.info(f"正在迁移私聊状态数据: {private_data_file}")
            with open(private_data_file, "r", encoding="utf8") as f:
                private_data = json.load(f)
            
            migrated_users = 0
            for uid_str, state_data in private_data.items():
                try:
                    uid = int(uid_str)
                    # 检查是否已存在
                    existing = await PrivateChatState.get_or_none(user_id=uid)
                    if existing:
                        logger.debug(f"用户 {uid} 已存在于数据库，跳过迁移")
                        continue
                    
                    # 创建新的状态记录
                    await PrivateChatState.create(
                        user_id=uid,
                        preset_name=state_data.get("preset", "off"),
                        user_prompt=state_data.get("group_prompt"),  # JSON 中存的是 group_prompt
                        output_reasoning_content=state_data.get("output_reasoning_content", False),
                        last_active=datetime.fromtimestamp(state_data.get("last_active", datetime.now().timestamp())),
                    )
                    
                    # 创建历史记录
                    messages = state_data.get("history", [])
                    if messages:
                        await ChatHistory.create(
                            user_id=uid,
                            is_private=True,
                            messages_json=ChatHistory.serialize_messages(messages),
                        )
                    
                    migrated_users += 1
                    logger.debug(f"已迁移用户 {uid}（{len(messages)} 条消息）")
                
                except Exception as e:
                    logger.error(f"迁移用户 {uid_str} 失败: {e}")
            
            logger.info(f"成功迁移 {migrated_users} 个用户的私聊状态")
        
        except Exception as e:
            logger.error(f"迁移私聊状态失败: {e}")
    
    logger.info("JSON 迁移完成")


async def backup_json_files():
    """备份旧的 JSON 文件"""
    if not data_file:
        return
    
    if os.path.exists(data_file):
        backup_file = f"{data_file}.backup"
        try:
            os.rename(data_file, backup_file)
            logger.info(f"已备份群组状态文件: {backup_file}")
        except Exception as e:
            logger.warning(f"备份文件失败: {e}")
    
    if private_data_file and os.path.exists(private_data_file):
        backup_file = f"{private_data_file}.backup"
        try:
            os.rename(private_data_file, backup_file)
            logger.info(f"已备份私聊状态文件: {backup_file}")
        except Exception as e:
            logger.warning(f"备份文件失败: {e}")
