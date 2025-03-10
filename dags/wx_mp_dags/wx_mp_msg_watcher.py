#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号消息监听处理DAG

功能：
1. 监听并处理来自webhook的微信公众号消息
2. 通过AI助手回复用户消息

特点：
1. 由webhook触发，不进行定时调度
2. 最大并发运行数为50
3. 支持消息分发到其他DAG处理
"""

# 标准库导入
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from threading import Thread

# 第三方库导入
import requests
from pydub import AudioSegment

# Airflow相关导入
from airflow import DAG
from airflow.api.common.trigger_dag import trigger_dag
from airflow.exceptions import AirflowException
from airflow.models import DagRun
from airflow.models.dagrun import DagRun
from airflow.models.variable import Variable
from airflow.hooks.base import BaseHook
from airflow.operators.python import BranchPythonOperator, PythonOperator
from airflow.utils.session import create_session
from airflow.utils.state import DagRunState

# 自定义库导入
from utils.dify_sdk import DifyAgent
from utils.redis import RedisLock
from utils.wechat_mp_channl import WeChatMPBot
from utils.tts import text_to_speech


DAG_ID = "wx_mp_msg_watcher"


def process_wx_message(**context):
    """
    处理微信公众号消息的任务函数, 消息分发到其他DAG处理
    
    Args:
        **context: Airflow上下文参数，包含dag_run等信息
    """
    # 打印当前python运行的path
    print(f"当前python运行的path: {os.path.abspath(__file__)}")

    # 获取传入的消息数据
    dag_run = context.get('dag_run')
    if not (dag_run and dag_run.conf):
        print("[WATCHER] 没有收到消息数据")
        return
        
    message_data = dag_run.conf
    print("--------------------------------")
    print(json.dumps(message_data, ensure_ascii=False, indent=2))
    print("--------------------------------")

    # 获取用户信息(注意，微信公众号并未提供详细的用户消息）
    mp_bot = WeChatMPBot(appid=Variable.get("WX_MP_APP_ID"), appsecret=Variable.get("WX_MP_SECRET"))
    user_info = mp_bot.get_user_info(message_data.get('FromUserName'))
    print(f"FromUserName: {message_data.get('FromUserName')}, 用户信息: {user_info}")
    # user_info = mp_bot.get_user_info(message_data.get('ToUserName'))
    # print(f"ToUserName: {message_data.get('ToUserName')}, 用户信息: {user_info}")
    
    
    # 判断消息类型
    msg_type = message_data.get('MsgType')
    
    if msg_type == 'text':
        return ['handler_text_msg', 'save_msg_to_mysql']
    elif msg_type == 'image':
        return ['handler_image_msg', 'save_msg_to_mysql']
    elif msg_type == 'voice':
        return ['handler_voice_msg', 'save_msg_to_mysql']
    else:
        print(f"[WATCHER] 不支持的消息类型: {msg_type}")
        return []


def handler_text_msg(**context):
    """
    处理文本类消息, 通过Dify的AI助手进行聊天, 并回复微信公众号消息
    """
    # 获取传入的消息数据
    message_data = context.get('dag_run').conf
    
    # 提取微信公众号消息的关键信息
    to_user_name = message_data.get('ToUserName')  # 公众号原始ID
    from_user_name = message_data.get('FromUserName')  # 发送者的OpenID
    create_time = message_data.get('CreateTime')  # 消息创建时间
    content = message_data.get('Content')  # 消息内容
    msg_id = message_data.get('MsgId')  # 消息ID
    
    print(f"收到来自 {from_user_name} 的消息: {content}")
    
    # 获取微信公众号配置
    appid = Variable.get("WX_MP_APP_ID", default_var="")
    appsecret = Variable.get("WX_MP_SECRET", default_var="")
    
    if not appid or not appsecret:
        print("[WATCHER] 微信公众号配置缺失")
        return
    
    # 初始化微信公众号机器人
    mp_bot = WeChatMPBot(appid=appid, appsecret=appsecret)
    
    # 初始化dify
    dify_agent = DifyAgent(api_key=Variable.get("LUCYAI_DIFY_API_KEY"), base_url=Variable.get("DIFY_BASE_URL"))
    
    # 获取会话ID
    conversation_id = dify_agent.get_conversation_id_for_user(from_user_name)
    
    # 获取AI回复
    full_answer, metadata = dify_agent.create_chat_message_stream(
        query=content,
        user_id=from_user_name,
        conversation_id=conversation_id,
        inputs={
            "platform": "wechat_mp",
            "user_id": from_user_name,
            "msg_id": msg_id
        }
    )
    print(f"full_answer: {full_answer}")
    print(f"metadata: {metadata}")
    response = full_answer
    
    if not conversation_id:
        # 新会话，重命名会话
        try:
            conversation_id = metadata.get("conversation_id")
            dify_agent.rename_conversation(conversation_id, f"微信公众号用户_{from_user_name[:8]}", "公众号对话")
        except Exception as e:
            print(f"[WATCHER] 重命名会话失败: {e}")
        
        # 保存会话ID
        conversation_infos = Variable.get("wechat_mp_conversation_infos", default_var={}, deserialize_json=True)
        conversation_infos[from_user_name] = conversation_id
        Variable.set("wechat_mp_conversation_infos", conversation_infos, serialize_json=True)
    
    # 发送回复消息
    try:
        # 将长回复拆分成多条消息发送
        for response_part in re.split(r'\\n\\n|\n\n', response):
            response_part = response_part.replace('\\n', '\n')
            if response_part.strip():  # 确保不发送空消息
                mp_bot.send_text_message(from_user_name, response_part)
                time.sleep(0.5)  # 避免发送过快
        
        # 记录消息已被成功回复
        dify_msg_id = metadata.get("message_id")
        if dify_msg_id:
            dify_agent.create_message_feedback(
                message_id=dify_msg_id, 
                user_id=from_user_name, 
                rating="like", 
                content="微信公众号自动回复成功"
            )

        # 保存消息到MySQL
        save_msg_to_mysql(context)

    except Exception as error:
        print(f"[WATCHER] 发送消息失败: {error}")
        # 记录消息回复失败
        dify_msg_id = metadata.get("message_id")
        if dify_msg_id:
            dify_agent.create_message_feedback(
                message_id=dify_msg_id, 
                user_id=from_user_name, 
                rating="dislike", 
                content=f"微信公众号自动回复失败, {error}"
            )
    
    # 打印会话消息
    messages = dify_agent.get_conversation_messages(conversation_id, from_user_name)
    print("-"*50)
    for msg in messages:
        print(msg)
    print("-"*50)


def save_msg_to_mysql(**context):
    """
    保存消息到MySQL
    """
    # 获取传入的消息数据
    message_data = context.get('dag_run').conf
    if not message_data:
        print("[DB_SAVE] 没有收到消息数据")
        return
    
    # 提取消息信息
    from_user_name = message_data.get('from_user_name', '')
    from_user_id = message_data.get('from_user_id', '')
    to_user_name = message_data.get('to_user_name', '')
    to_user_id = message_data.get('to_user_id', '')
    msg_id = message_data.get('id', '')
    msg_type = message_data.get('type', 0)
    content = message_data.get('content', '')
    msg_timestamp = message_data.get('ts', 0)
    
    # 获取微信账号信息
    wx_account_info = context.get('task_instance').xcom_pull(key='wx_account_info')
    if not wx_account_info:
        print("[DB_SAVE] 没有获取到微信公众号账号信息")
        return
    
    
    # 消息类型名称
    msg_type_name = WX_MSG_TYPES.get(msg_type, f"未知类型({msg_type})")
    
    # 转换时间戳为datetime
    if msg_timestamp:
        msg_datetime = datetime.fromtimestamp(msg_timestamp)
    else:
        msg_datetime = datetime.now()
    
    # 聊天记录的创建数据包
    create_table_sql = """CREATE TABLE IF NOT EXISTS `wx_mp_chat_records` (
        `id` bigint(20) NOT NULL AUTO_INCREMENT,
        `from_user_id` varchar(64) NOT NULL COMMENT '发送者ID',
        `from_user_name` varchar(128) DEFAULT NULL COMMENT '发送者名称',
        `to_user_id` varchar(128) DEFAULT NULL COMMENT '接收者ID',
        `to_user_name` varchar(128) DEFAULT NULL COMMENT '接收者名称',
        `msg_id` varchar(64) NOT NULL COMMENT '微信消息ID',        
        `msg_type` int(11) NOT NULL COMMENT '消息类型',
        `msg_type_name` varchar(64) DEFAULT NULL COMMENT '消息类型名称',
        `content` text COMMENT '消息内容',
        `msg_timestamp` bigint(20) DEFAULT NULL COMMENT '消息时间戳',
        `msg_datetime` datetime DEFAULT NULL COMMENT '消息时间',
        `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
        `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
        PRIMARY KEY (`id`),
        UNIQUE KEY `uk_msg_id` (`msg_id`),
        KEY `idx_to_user_id` (`to_user_id`),
        KEY `idx_from_user_id` (`from_user_id`),
        KEY `idx_msg_datetime` (`msg_datetime`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='微信公众号聊天记录';
    """
    
    # 插入数据SQL
    insert_sql = """INSERT INTO `wx_mp_chat_records` 
    (from_user_id, from_user_name, to_user_id, to_user_name, msg_id, 
    msg_type, msg_type_name, content, msg_timestamp, msg_datetime) 
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE 
    content = VALUES(content),
    from_user_name = VALUES(from_user_name),
    to_user_name = VALUES(to_user_name),
    updated_at = CURRENT_TIMESTAMP
    """
    
    db_conn = None
    cursor = None
    try:
        # 使用get_hook函数获取数据库连接
        db_hook = BaseHook.get_connection("wx_db").get_hook()
        db_conn = db_hook.get_conn()
        cursor = db_conn.cursor()
        
        # 创建表（如果不存在）
        cursor.execute(create_table_sql)
        
        # 插入数据
        cursor.execute(insert_sql, (
            msg_id,             
            from_user_name,
            to_user_name,
            msg_type,
            msg_type_name,
            content,
            msg_timestamp,
            msg_datetime
        ))
        
        # 提交事务
        db_conn.commit()
        print(f"[DB_SAVE] 成功保存消息到数据库: {msg_id}")
    except Exception as e:
        print(f"[DB_SAVE] 保存消息到数据库失败: {e}")
        if db_conn:
            try:
                db_conn.rollback()
            except:
                pass
    finally:
        # 关闭连接
        if cursor:
            try:
                cursor.close()
            except:
                pass
        if db_conn:
            try:
                db_conn.close()
            except:
                pass


def handler_image_msg(**context):
    """
    处理图片类消息, 通过Dify的AI助手进行聊天, 并回复微信公众号消息
    """
    # 获取传入的消息数据
    message_data = context.get('dag_run').conf
    
    # 提取微信公众号消息的关键信息
    to_user_name = message_data.get('ToUserName')  # 公众号原始ID
    from_user_name = message_data.get('FromUserName')  # 发送者的OpenID
    create_time = message_data.get('CreateTime')  # 消息创建时间
    pic_url = message_data.get('PicUrl')  # 图片链接
    media_id = message_data.get('MediaId')  # 图片消息媒体id
    msg_id = message_data.get('MsgId')  # 消息ID
    
    print(f"收到来自 {from_user_name} 的图片消息，MediaId: {media_id}, PicUrl: {pic_url}")
    
    # 获取微信公众号配置
    appid = Variable.get("WX_MP_APP_ID", default_var="")
    appsecret = Variable.get("WX_MP_SECRET", default_var="")
    
    if not appid or not appsecret:
        print("[WATCHER] 微信公众号配置缺失")
        return
    
    # 初始化微信公众号机器人
    mp_bot = WeChatMPBot(appid=appid, appsecret=appsecret)
    
    # 初始化dify
    dify_agent = DifyAgent(api_key=Variable.get("LUCYAI_DIFY_API_KEY"), base_url=Variable.get("DIFY_BASE_URL"))
    
    # 获取会话ID
    conversation_id = dify_agent.get_conversation_id_for_user(from_user_name)
    
    # 准备问题内容
    query = "这是一张图片，请描述一下图片内容并给出你的分析"
    
    # 创建临时目录用于保存下载的图片
    import tempfile
    import os
    from datetime import datetime
    
    temp_dir = tempfile.gettempdir()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    img_file_path = os.path.join(temp_dir, f"wx_img_{from_user_name}_{timestamp}.jpg")
    
    try:
        # 下载图片
        if pic_url:
            # 如果有直接的图片URL，使用URL下载
            img_response = requests.get(pic_url)
            with open(img_file_path, 'wb') as img_file:
                img_file.write(img_response.content)
        else:
            # 否则使用media_id获取临时素材
            mp_bot.download_temporary_media(media_id, img_file_path)
        
        print(f"[WATCHER] 图片已保存到: {img_file_path}")
        
        # 获取AI回复（带有图片分析）
        # 注意：这里假设Dify支持图片处理，如果不支持，需要修改为其他图像处理API
        full_answer, metadata = dify_agent.create_chat_message_stream(
            query=query,
            user_id=from_user_name,
            conversation_id=conversation_id,
            inputs={
                "platform": "wechat_mp",
                "user_id": from_user_name,
                "msg_id": msg_id,
                "image_path": img_file_path  # 传递图片路径给Dify
            }
        )
        print(f"full_answer: {full_answer}")
        print(f"metadata: {metadata}")
        response = full_answer
        
        # 处理会话ID相关逻辑
        if not conversation_id:
            # 新会话，重命名会话
            try:
                conversation_id = metadata.get("conversation_id")
                dify_agent.rename_conversation(conversation_id, f"微信公众号用户_{from_user_name[:8]}", "公众号图片对话")
            except Exception as e:
                print(f"[WATCHER] 重命名会话失败: {e}")
            
            # 保存会话ID
            conversation_infos = Variable.get("wechat_mp_conversation_infos", default_var={}, deserialize_json=True)
            conversation_infos[from_user_name] = conversation_id
            Variable.set("wechat_mp_conversation_infos", conversation_infos, serialize_json=True)
        
        # 发送回复消息
        try:
            # 将长回复拆分成多条消息发送
            for response_part in re.split(r'\\n\\n|\n\n', response):
                response_part = response_part.replace('\\n', '\n')
                if response_part.strip():  # 确保不发送空消息
                    mp_bot.send_text_message(from_user_name, response_part)
                    time.sleep(0.5)  # 避免发送过快
            
            # 记录消息已被成功回复
            dify_msg_id = metadata.get("message_id")
            if dify_msg_id:
                dify_agent.create_message_feedback(
                    message_id=dify_msg_id, 
                    user_id=from_user_name, 
                    rating="like", 
                    content="微信公众号图片消息自动回复成功"
                )
        except Exception as error:
            print(f"[WATCHER] 发送消息失败: {error}")
            # 记录消息回复失败
            dify_msg_id = metadata.get("message_id")
            if dify_msg_id:
                dify_agent.create_message_feedback(
                    message_id=dify_msg_id, 
                    user_id=from_user_name, 
                    rating="dislike", 
                    content=f"微信公众号图片消息自动回复失败, {error}"
                )
    except Exception as e:
        print(f"[WATCHER] 处理图片消息失败: {e}")
        # 发送错误提示给用户
        try:
            mp_bot.send_text_message(from_user_name, f"很抱歉，无法处理您的图片，发生了以下错误：{str(e)}")
        except Exception as send_error:
            print(f"[WATCHER] 发送错误提示失败: {send_error}")
    finally:
        # 清理临时文件
        try:
            if os.path.exists(img_file_path):
                os.remove(img_file_path)
                print(f"[WATCHER] 临时图片文件已删除: {img_file_path}")
        except Exception as e:
            print(f"[WATCHER] 删除临时文件失败: {e}")


def handler_voice_msg(**context):
    """
    处理语音类消息, 通过Dify的AI助手进行聊天, 并回复微信公众号消息
    
    处理流程:
    1. 接收用户发送的语音消息
    2. 下载语音文件并保存到临时目录
    3. 使用语音转文字API将语音内容转为文本
    4. 将转换后的文本发送给Dify AI进行处理
    5. 将AI回复的文本转换为语音（使用阿里云文字转语音）
    6. 上传语音到微信公众号获取media_id
    7. 发送语音回复给用户
    8. 同时发送文字回复作为备份
    """
    # 获取传入的消息数据
    message_data = context.get('dag_run').conf
    
    # 提取微信公众号消息的关键信息
    to_user_name = message_data.get('ToUserName')  # 公众号原始ID
    from_user_name = message_data.get('FromUserName')  # 发送者的OpenID
    create_time = message_data.get('CreateTime')  # 消息创建时间
    media_id = message_data.get('MediaId')  # 语音消息媒体id
    format_type = message_data.get('Format')  # 语音格式，如amr，speex等
    msg_id = message_data.get('MsgId')  # 消息ID
    media_id_16k = message_data.get('MediaId16K')  # 16K采样率语音消息媒体id
    
    print(f"收到来自 {from_user_name} 的语音消息，MediaId: {media_id}, Format: {format_type}, MediaId16K: {media_id_16k}")
    
    # 获取微信公众号配置
    appid = Variable.get("WX_MP_APP_ID", default_var="")
    appsecret = Variable.get("WX_MP_SECRET", default_var="")
    
    if not appid or not appsecret:
        print("[WATCHER] 微信公众号配置缺失")
        return
    
    # 初始化微信公众号机器人
    mp_bot = WeChatMPBot(appid=appid, appsecret=appsecret)
    
    # 初始化dify
    dify_agent = DifyAgent(api_key=Variable.get("LUCYAI_DIFY_API_KEY"), base_url=Variable.get("DIFY_BASE_URL"))
    
    # 获取会话ID
    conversation_id = dify_agent.get_conversation_id_for_user(from_user_name)
    
    # 创建临时目录用于保存下载的语音文件
    import tempfile
    import os
    from datetime import datetime
    
    temp_dir = tempfile.gettempdir()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    
    # 优先使用16K采样率的语音(如果有)
    voice_media_id = media_id_16k if media_id_16k else media_id
    voice_file_path = os.path.join(temp_dir, f"wx_voice_{from_user_name}_{timestamp}.{format_type.lower()}")
    
    try:
        # 1. 下载语音文件
        mp_bot.download_temporary_media(voice_media_id, voice_file_path)
        print(f"[WATCHER] 语音文件已保存到: {voice_file_path}")
        
        # 2. 语音转文字
        try:
            # 转换音频格式 - 如果是AMR格式，转换为WAV格式
            converted_file_path = None
            if format_type.lower() == 'amr':                
                # 创建转换后的文件路径
                converted_file_path = os.path.join(temp_dir, f"wx_voice_{from_user_name}_{timestamp}.wav")
                
                # 进行格式转换
                print(f"[WATCHER] 正在将AMR格式转换为WAV格式...")
                sound = AudioSegment.from_file(voice_file_path, format="amr")
                sound.export(converted_file_path, format="wav")
                print(f"[WATCHER] 音频格式转换成功，WAV文件保存在: {converted_file_path}")
                
            # 使用可能转换过的文件路径进行语音转文字
            file_to_use = converted_file_path if converted_file_path else voice_file_path
            print(f"[WATCHER] 用于语音转文字的文件: {file_to_use}")
            
            transcribed_text = dify_agent.audio_to_text(file_to_use)
            print(f"[WATCHER] 语音转文字结果: {transcribed_text}")
            
            if not transcribed_text.strip():
                raise Exception("语音转文字结果为空")
        except Exception as e:
            print(f"[WATCHER] 语音转文字失败: {e}")
            # 如果语音转文字失败，使用默认文本
            transcribed_text = "您发送了一条语音消息，但我无法识别内容。请问您想表达什么？"
        
        # 3. 发送转写的文本到Dify
        full_answer, metadata = dify_agent.create_chat_message_stream(
            query=transcribed_text,  # 使用转写的文本
            user_id=from_user_name,
            conversation_id=conversation_id,
            inputs={
                "platform": "wechat_mp",
                "user_id": from_user_name,
                "msg_id": msg_id,
                "is_voice_msg": True,
                "transcribed_text": transcribed_text
            }
        )
        print(f"full_answer: {full_answer}")
        print(f"metadata: {metadata}")
        response = full_answer
        
        # 处理会话ID相关逻辑
        if not conversation_id:
            # 新会话，重命名会话
            try:
                conversation_id = metadata.get("conversation_id")
                dify_agent.rename_conversation(conversation_id, f"微信公众号用户_{from_user_name[:8]}", "公众号语音对话")
            except Exception as e:
                print(f"[WATCHER] 重命名会话失败: {e}")
            
            # 保存会话ID
            conversation_infos = Variable.get("wechat_mp_conversation_infos", default_var={}, deserialize_json=True)
            conversation_infos[from_user_name] = conversation_id
            Variable.set("wechat_mp_conversation_infos", conversation_infos, serialize_json=True)
        
        # 4. 使用阿里云的文字转语音功能
        audio_response_path = os.path.join(temp_dir, f"wx_audio_response_{from_user_name}_{timestamp}.mp3")
        try:
            # 调用阿里云的TTS服务 - 直接生成MP3格式
            success, _ = text_to_speech(
                text=response, 
                output_path=audio_response_path, 
                model="cosyvoice-v2", 
                voice="longxiaoxia_v2"
            )
            
            if not success:
                raise Exception("文字转语音失败")
                
            print(f"[WATCHER] 文字转语音成功，保存到: {audio_response_path}")
            
            # 5. 上传语音文件到微信获取media_id
            upload_result = mp_bot.upload_temporary_media("voice", audio_response_path)
            response_media_id = upload_result.get('media_id')
            print(f"[WATCHER] 语音文件上传成功，media_id: {response_media_id}")
            
            # 6. 发送语音回复
            mp_bot.send_voice_message(from_user_name, response_media_id)
            print(f"[WATCHER] 语音回复发送成功")
            
            # 语音回复成功，不需要发送文字回复
            send_text_response = False
                
        except Exception as e:
            print(f"[WATCHER] 语音回复失败: {e}")
            send_text_response = True
        
        # 只有在语音回复失败时才发送文字回复
        if send_text_response:
            try:
                # 将长回复拆分成多条消息发送
                for response_part in re.split(r'\\n\\n|\n\n', response):
                    response_part = response_part.replace('\\n', '\n')
                    if response_part.strip():  # 确保不发送空消息
                        mp_bot.send_text_message(from_user_name, response_part)
                        time.sleep(0.5)  # 避免发送过快
                        
                print(f"[WATCHER] 文字回复发送成功")
            except Exception as text_error:
                print(f"[WATCHER] 文字回复发送失败: {text_error}")
        
        # 记录消息已被成功回复
        dify_msg_id = metadata.get("message_id")
        if dify_msg_id:
            dify_agent.create_message_feedback(
                message_id=dify_msg_id, 
                user_id=from_user_name, 
                rating="like", 
                content="微信公众号语音消息自动回复成功"
            )
    except Exception as e:
        print(f"[WATCHER] 处理语音消息失败: {e}")
        # 发送错误提示给用户
        try:
            mp_bot.send_text_message(from_user_name, f"很抱歉，无法处理您的语音消息，发生了以下错误：{str(e)}")
        except Exception as send_error:
            print(f"[WATCHER] 发送错误提示失败: {send_error}")
    finally:
        # 清理临时文件
        try:
            # 定义需要清理的所有临时文件
            temp_files = []
            if 'voice_file_path' in locals() and voice_file_path:
                temp_files.append(voice_file_path)
            if 'audio_response_path' in locals() and audio_response_path:
                temp_files.append(audio_response_path)
            if 'converted_file_path' in locals() and converted_file_path:
                temp_files.append(converted_file_path)
            
            # 删除所有临时文件
            for file_path in temp_files:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    print(f"[WATCHER] 临时文件已删除: {file_path}")
        except Exception as e:
            print(f"[WATCHER] 删除临时文件失败: {e}")


def handler_file_msg(**context):
    """
    处理文件类消息, 通过Dify的AI助手进行聊天, 并回复微信公众号消息
    """
    # TODO(claude89757): 处理文件类消息, 通过Dify的AI助手进行聊天, 并回复微信公众号消息
    pass


# 创建DAG
dag = DAG(
    dag_id=DAG_ID,
    default_args={'owner': 'claude89757'},
    start_date=datetime(2024, 1, 1),
    schedule_interval=None,
    max_active_runs=50,
    catchup=False,
    tags=['微信公众号'],
    description='微信公众号消息监控',
)

# 创建处理消息的任务
process_message_task = BranchPythonOperator(
    task_id='process_wx_message',
    python_callable=process_wx_message,
    provide_context=True,
    dag=dag
)

# 创建处理文本消息的任务
handler_text_msg_task = PythonOperator(
    task_id='handler_text_msg',
    python_callable=handler_text_msg,
    provide_context=True,
    dag=dag
)

# 创建处理图片消息的任务
handler_image_msg_task = PythonOperator(
    task_id='handler_image_msg',
    python_callable=handler_image_msg,
    provide_context=True,
    dag=dag
)

# 创建处理语音消息的任务
handler_voice_msg_task = PythonOperator(
    task_id='handler_voice_msg',
    python_callable=handler_voice_msg,
    provide_context=True,
    dag=dag
)

# 创建保存消息到MySQL的任务
save_msg_to_mysql_task = PythonOperator(
    task_id='save_msg_to_mysql',
    python_callable=save_msg_to_mysql,
    provide_context=True,
    dag=dag
)

# 设置任务依赖关系
process_message_task >> [handler_text_msg_task, handler_image_msg_task, handler_voice_msg_task, save_msg_to_mysql_task]
