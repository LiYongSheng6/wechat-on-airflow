#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI聊天处理DAG

功能描述:
    - 处理来自微信的AI助手对话请求
    - 使用OpenAI的GPT模型生成回复
    - 将回复发送回微信对话

主要组件:
    1. 微信消息处理
    2. OpenAI API调用
    3. 系统配置管理

触发方式:
    - 由wx_msg_watcher触发，不进行定时调度
    - 最大并发运行数为3
    - 支持失败重试
"""

# 标准库导入
import json
import time
import re
import os
from datetime import datetime, timedelta

# 第三方库导入
from airflow import DAG
from airflow.models import Variable
from airflow.operators.python import PythonOperator, BranchPythonOperator
from airflow.utils.state import DagRunState

# 自定义库导入
from utils.wechat_channl import send_wx_msg_by_wcf_api
from utils.llm_channl import get_llm_response


# --- 意图分析函数 --- 

def analyze_intent(**context) -> str:
    """
    分析用户意图
    """
    message_data = context.get('dag_run').conf
    print(f"[INTENT] 收到消息数据: {json.dumps(message_data, ensure_ascii=False)}")

    content = message_data['content']
    sender = message_data['sender']  
    room_id = message_data['roomid']  
    msg_ts = message_data['ts']

    # 关联该消息发送者的最近1分钟内的消息
    room_msg_data = Variable.get(f'{room_id}_msg_data', default_var={}, deserialize_json=True)
    one_minute_before_timestamp = msg_ts - 60  # 60秒前的时间戳
    recent_messages = [
        msg for msg in room_msg_data.get(sender, [])
        if msg.get('timestamp') and 
        msg['timestamp'] > one_minute_before_timestamp
    ]
    recent_msg_count = len(recent_messages)
    print(f"[INTENT] 发送者 {sender} 在最近1分钟内发送了 {recent_msg_count} 条消息")
    
    # 结合最近1分钟内的消息，生成完整的对话内容
    content_list = []
    if recent_messages:
        for msg in recent_messages[:5]:
            content = msg['content'].replace('@Zacks', '').strip()
            content_list.append(content)
    else:
        content = content.replace('@Zacks', '').strip()
        content_list.append(content)
    content = "\n".join(list(set(content_list)))
    print(f"[INTENT] 完整对话内容: {content}")

    # 调用AI接口进行意图分析
    dagrun_state = context.get('dag_run').get_state()
    if dagrun_state == DagRunState.RUNNING:
        system_prompt = """你是一个聊天意图分析专家，请根据对话内容分析用户的意图。
意图类型分为两大类:
1. chat - 普通聊天，包括问候、闲聊等
2. product - 产品咨询，包括产品功能、价格、使用方法等咨询

请返回JSON格式数据，包含以下字段:
- type: 意图类型，只能是chat或product
- description: 意图的具体描述

示例:
{
    "type": "chat",
    "description": "用户在进行日常问候"
}
或
{
    "type": "product", 
    "description": "用户在咨询产品价格"
}"""
        response = get_llm_response(content, system_prompt=system_prompt)
        try:
            # 使用正则提取json格式内容
            json_pattern = r'\{[^{}]*\}'
            json_match = re.search(json_pattern, response)
            if json_match:
                intent = json.loads(json_match.group())
            else:
                # 如果没有找到json格式,使用默认结构
                intent = {
                    "type": "chat",
                    "description": response
                }
        except (json.JSONDecodeError, re.error):
            # 解析失败时使用默认结构
            intent = {
                "type": "chat", 
                "description": response
            }
    else:
        raise Exception(f"当前任务状态: {dagrun_state}, 停止意图分析")

    print(f"[INTENT] 意图分析结果: {intent}")

    # 缓存聊天内容到xcom, 后续任务使用
    context['ti'].xcom_push(key='content', value=content)
    context['ti'].xcom_push(key='room_id', value=room_id)

    # 根据意图类型选择下一个任务
    next_dag_task_id = "process_ai_chat" if intent['type'] == "chat" else "process_ai_product"
    return next_dag_task_id


def process_ai_chat(**context):
    """处理AI聊天的主任务函数"""
    # 获取聊天内容(聚合后的)
    content = context['ti'].xcom_pull(key='content')
    room_id = context['ti'].xcom_pull(key='room_id')
    sender = context['ti'].xcom_pull(key='sender')

    # 最近5分钟内的10条对话
    room_msg_data = Variable.get(f'{room_id}_msg_data', default_var={}, deserialize_json=True)
    five_minutes_ago_timestamp = datetime.now().timestamp() - 300  # 5分钟前的时间戳
    recent_messages = [
        msg for msg in room_msg_data.get(sender, [])
        if msg.get('timestamp') and 
        msg['timestamp'] > five_minutes_ago_timestamp
    ]
    history_reply = ""
    for msg in recent_messages[:10]:
        if msg['sender'] == sender:
            history_reply += f"用户: {msg['content']}\n"
        elif msg['sender'] == f"TO_{sender}_BY_AI":
            history_reply += f"AI: {msg['content']}\n"
        else:
            history_reply += f"其他人: {msg['content']}\n"
    print(f"[PRODUCT] 历史对话: {history_reply}")

    if history_reply:
        system_prompt = f"""你是一个聊天助手，请根据对话内容生成回复。

    历史对话, 作为参考:
    {history_reply}
    """
    else:
        system_prompt = """你是一个聊天助手，请根据对话内容生成回复。"""

    # 调用AI接口获取回复
    dagrun_state = context.get('dag_run').get_state()  # 获取实时状态
    if dagrun_state == DagRunState.RUNNING:
        response = get_llm_response(content, system_prompt=system_prompt)
        print(f"[CHAT] AI回复: {response}")
    else:
        print(f"[CHAT] 当前任务状态: {dagrun_state}, 直接返回")
        return

    # 缓存回复内容到xcom, 后续任务使用
    context['ti'].xcom_push(key='raw_llm_response', value=response)


def process_ai_product(**context):
    """处理AI产品咨询的主任务函数"""
    # 获取聊天内容(聚合后的)
    content = context['ti'].xcom_pull(key='content')
    room_id = context['ti'].xcom_pull(key='room_id')
    sender = context['ti'].xcom_pull(key='sender')

    # 提取@Zacks后的实际问题内容
    if not content:
        print("[CHAT] 没有检测到实际问题内容")
        return
    
    # 最近5分钟内的10条对话
    room_msg_data = Variable.get(f'{room_id}_msg_data', default_var={}, deserialize_json=True)
    five_minutes_ago_timestamp = datetime.now().timestamp() - 300  # 5分钟前的时间戳
    recent_messages = [
        msg for msg in room_msg_data.get(sender, [])
        if msg.get('timestamp') and 
        msg['timestamp'] > five_minutes_ago_timestamp
    ]
    history_reply = ""
    for msg in recent_messages[:10]:
        if msg['sender'] == sender:
            history_reply += f"用户: {msg['content']}\n"
        elif msg['sender'] == f"TO_{sender}_BY_AI":
            history_reply += f"AI: {msg['content']}\n"
        else:
            history_reply += f"其他人: {msg['content']}\n"
    print(f"[PRODUCT] 历史对话: {history_reply}")

    system_prompt = f"""
    你是一个专业的产品经理助手。你需要:
    1. 仔细倾听用户的问题和需求
    2. 提供专业、具体和可执行的建议
    3. 回答要简洁明了,避免过于冗长
    4. 如果用户问题不够清晰,要主动询问更多细节
    5. 保持友好和专业的语气
    6. 如果涉及具体数据,要说明数据来源
    7. 如果不确定的内容,要诚实说明
    8. 避免过度承诺或夸大其词
    9. 适时使用专业术语,但要确保用户能理解
    10. 在合适的时候提供相关的最佳实践建议

    历史对话, 作为参考:
    {history_reply}
    """

    # 调用AI接口获取回复
    dagrun_state = context.get('dag_run').get_state()  # 获取实时状态
    if dagrun_state == DagRunState.RUNNING:
        response = get_llm_response(content, system_prompt=system_prompt)
        print(f"[CHAT] AI回复: {response}")
    else:
        print(f"[CHAT] 当前任务状态: {dagrun_state}, 直接返回")
        return
    
    # 缓存回复内容到xcom, 后续任务使用
    context['ti'].xcom_push(key='raw_llm_response', value=response)


def humanize_reply(**context):
    """
    微信聊天的拟人化回复
    """
    # 获取消息数据
    message_data = context.get('dag_run').conf
    sender = message_data.get('sender', '')  # 发送者ID
    room_id = message_data.get('roomid', '')  # 群聊ID
    is_group = message_data.get('is_group', False)  # 是否群聊
    source_ip = message_data.get('source_ip', '')  # 获取源IP, 用于发送消息

    # 获取AI回复内容
    raw_llm_response = context['ti'].xcom_pull(key='raw_llm_response')

    # 拟人化回复的系统提示词
    system_prompt = """你是一个聊天助手，需要将AI的回复转换成更自然的聊天风格。

请遵循以下规则:
1. 语气要自然友好，像真人一样：
   - 适量使用emoji表情，但不要过度
   - 根据语境使用合适的语气词(啊、呢、哦、嘿等)
   - 可以用一些网络用语(比如"稍等哈"、"木问题"等)，但要得体
   - 表达要生动活泼，避免刻板

2. 消息长度和分段：
   - 单条消息控制在合适长度，不要太长
   - 根据内容自然分段，不要机械分割
   - 重要信息要突出，可以用符号标记

3. 回复节奏：
   - 短消息间隔1-2秒
   - 长消息间隔2-4秒
   - 思考或转折处可以停顿3-5秒
   - 通过delay字段控制停顿时间

请将回复转换为以下JSON格式：
{
    "messages": [
        {
            "content": "消息内容",
            "delay": 停顿秒数
        }
    ]
}

示例输入1(技术问题)：
"要在Linux系统中查看CPU使用率，可以使用top命令。top命令会显示实时的系统资源使用情况，包括CPU、内存等信息。使用方法是直接在终端输入top即可。"

示例输出1：
{
    "messages": [
        {
            "content": "让我来告诉你查看CPU使用率的小技巧~ 💻",
            "delay": 1.5
        },
        {
            "content": "其实超级简单，在终端里敲个top命令就搞定啦！",
            "delay": 2
        },
        {
            "content": "回车之后你就能看到实时的系统资源使用情况了，CPU、内存啥的一目了然 👀",
            "delay": 2.5
        }
    ]
}

示例输入2(闲聊)：
"今天天气真不错，阳光明媚，很适合出去散步。"

示例输出2：
{
    "messages": [
        {
            "content": "是呀是呀☀️ 这么好的天气窝在家里太可惜啦~",
            "delay": 1.8
        },
        {
            "content": "出去溜达溜达，晒晒太阳，心情都会变好呢！🚶‍♂️✨",
            "delay": 2
        }
    ]
}"""

    # 调用AI接口获取回复
    dagrun_state = context.get('dag_run').get_state()  # 获取实时状态
    if dagrun_state == DagRunState.RUNNING:
        humanized_response = get_llm_response(raw_llm_response, system_prompt=system_prompt)
        print(f"[CHAT] AI回复: {humanized_response}")

        # 提前将回复内容转换为JSON格式
        try:
            # 使用正则提取json格式内容
            json_pattern = r'\{[^{}]*\}'
            json_match = re.search(json_pattern, humanized_response)
            if json_match:
                data = json.loads(json_match.group())
            else:
                # 如果没有找到json格式,使用默认结构
                data = {"messages": [{"content": humanized_response, "delay": 1.5}]}
        except (json.JSONDecodeError, re.error):
            # 解析失败时使用默认结构
            data = {"messages": [{"content": humanized_response, "delay": 1.5}]}
    else:
        print(f"[CHAT] 当前任务状态: {dagrun_state}, 直接返回")
        return

    # 消息发送前，确认当前任务还是运行中，才发送消息
    dagrun_state = context.get('dag_run').get_state()  # 获取实时状态
    if dagrun_state == DagRunState.RUNNING:
        # 聊天的历史消息
        room_msg_data = Variable.get(f'{room_id}_msg_data', default_var={})
        for message in data['messages']:
            send_wx_msg_by_wcf_api(wcf_ip=source_ip, message=message['content'], receiver=room_id)

            # 缓存聊天的历史消息    
            simple_message_data = {
                'roomid': room_id,
                'sender': f"TO_{sender}_BY_AI",
                'id': "NULL",
                'content': message['content'],
                'is_group': is_group,
                'ts': datetime.now().timestamp(),
                'is_response_by_ai': True
            }
            room_msg_data[sender] = room_msg_data.get(sender, []) + [simple_message_data]
            Variable.set(f'{room_id}_msg_data', room_msg_data, serialize_json=True)

            # 延迟发送消息
            time.sleep(int(message['delay']))
    else:
        print(f"[CHAT] 当前任务状态: {dagrun_state}, 不发送消息")


# 创建DAG
dag = DAG(
    dag_id='zacks_ai_agent',
    default_args={
        'owner': 'claude89757',
        'depends_on_past': False,
        'email_on_failure': False,
        'email_on_retry': False,
        'retries': 0,
        'retry_delay': timedelta(minutes=1),
    },
    start_date=datetime(2024, 1, 1),
    schedule_interval=None,
    max_active_runs=10,
    catchup=False,
    tags=['AI助手'],
    description='处理AI聊天的DAG',
)


# 创建任务
analyze_intent_task = BranchPythonOperator(
    task_id='analyze_intent',
    python_callable=analyze_intent,
    provide_context=True,
    dag=dag,
)

process_ai_chat_task = PythonOperator(
    task_id='process_ai_chat',
    python_callable=process_ai_chat,
    provide_context=True,
    dag=dag,
)

process_ai_product_task = PythonOperator(
    task_id='process_ai_product',
    python_callable=process_ai_product,
    provide_context=True,
    dag=dag,
)

humanize_reply_task = PythonOperator(
    task_id='humanize_reply',
    python_callable=humanize_reply,
    provide_context=True,
    trigger_rule='none_failed',
    dag=dag,
)

# 设置任务依赖关系
analyze_intent_task >> [process_ai_chat_task, process_ai_product_task] >> humanize_reply_task