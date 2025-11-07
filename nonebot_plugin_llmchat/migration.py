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

from .config import Config
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
    
    total_migrated_groups = 0
    total_migrated_users = 0
    
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
        total_migrated_groups = migrated_groups
        
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
            total_migrated_users = migrated_users
        
        except Exception as e:
            logger.error(f"迁移私聊状态失败: {e}")
    
    # 迁移成功后，重命名 JSON 文件为 .migrated
    if total_migrated_groups > 0 or total_migrated_users > 0:
        logger.info("迁移成功，开始重命名 JSON 文件...")
        rename_json_files_to_migrated()
    
    logger.info(f"JSON 迁移完成（群组: {total_migrated_groups}，用户: {total_migrated_users}）")


def rename_json_files_to_migrated():
    """将已迁移的 JSON 文件重命名为 .migrated"""
    if not data_file:
        return
    
    if os.path.exists(data_file):
        migrated_file = f"{data_file}.migrated"
        try:
            os.rename(data_file, migrated_file)
            logger.info(f"已将群组状态文件重命名为: {migrated_file}")
        except Exception as e:
            logger.warning(f"重命名文件失败: {e}")
    
    if private_data_file and os.path.exists(private_data_file):
        migrated_file = f"{private_data_file}.migrated"
        try:
            os.rename(private_data_file, migrated_file)
            logger.info(f"已将私聊状态文件重命名为: {migrated_file}")
        except Exception as e:
            logger.warning(f"重命名文件失败: {e}")
