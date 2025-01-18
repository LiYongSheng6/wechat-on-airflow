#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信消息监听处理DAG

功能：
1. 监听并处理来自webhook的微信消息
2. 当收到@Zacks的消息时，触发AI聊天DAG

特点：
1. 由webhook触发，不进行定时调度
2. 最大并发运行数为10
3. 支持消息分发到其他DAG处理
"""

# 标准库导入
import json
import os
import re
from datetime import datetime, timedelta, timezone
import time

# Airflow相关导入
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.api.common.trigger_dag import trigger_dag
from airflow.models.dagrun import DagRun
from airflow.utils.state import DagRunState
from airflow.models.variable import Variable
from airflow.utils.session import create_session
from utils.wechat_channl import send_wx_msg_by_wcf_api


def process_wx_message(**context):
    """
    处理微信消息的任务函数, 消息分发到其他DAG处理
    
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
    print("[WATCHER] 收到微信消息:")
    print("[WATCHER] 消息类型:", message_data.get('type'))
    print("[WATCHER] 消息内容:", message_data.get('content'))
    print("[WATCHER] 发送者:", message_data.get('sender'))
    print("[WATCHER] 群聊ID:", message_data.get('roomid'))
    print("[WATCHER] 是否群聊:", message_data.get('is_group'))
    print("[WATCHER] 完整消息数据:")
    print("--------------------------------")
    print(json.dumps(message_data, ensure_ascii=False, indent=2))
    print("--------------------------------")
    
    # 检查是否需要触发AI聊天
    room_id = message_data.get('roomid')
    sender = message_data.get('sender')
    msg_id = message_data.get('id')
    msg_type = message_data.get('type')
    content = message_data.get('content', '')
    is_group = message_data.get('is_group', False)  # 是否群聊
    current_msg_timestamp = message_data.get('ts')
    source_ip = message_data.get('source_ip')

    # 分类处理
    if msg_type == 1 and (content.startswith('@Zacks') or not is_group):

        # 命令1：清理历史消息
        if content.replace('@Zacks', '').strip().lower() == 'clear':
            print("[命令] 清理历史消息")
            Variable.set(f'{room_id}_msg_data', [], serialize_json=True)
            send_wx_msg_by_wcf_api(wcf_ip=source_ip, message='[bot]已清理历史消息', receiver=sender)
            return

        # 缓存聊天的历史消息
        room_msg_data = Variable.get(f'{room_id}_msg_data', default_var=[], deserialize_json=True)
        simple_message_data = {
            'roomid': room_id,
            'sender': sender,
            'id': msg_id,
            'content': content,
            'is_group': is_group,
            'ts': current_msg_timestamp,
            'is_ai_msg': False
        }
        room_msg_data.append(simple_message_data)
        Variable.set(f'{room_id}_msg_data', room_msg_data, serialize_json=True)

        # 检查是否有来自相同roomid和sender的DAG正在运行
        active_runs = DagRun.find(
            dag_id='zacks_ai_agent',
            state=DagRunState.RUNNING,
        )
        same_room_sender_runs = [run for run in active_runs if run.run_id.startswith(f'{room_id}_{sender}_')]   
        if same_room_sender_runs:
            print(f"[WATCHER] 发现来自相同room_id和sender的DAG正在运行, run_id: {same_room_sender_runs}")
            # 使用数据库会话检查并强制结束未响应的DAG任务
            with create_session() as session:
                for run in same_room_sender_runs:
                    # 从数据库中查询最新的任务状态
                    updated_run = session.query(DagRun).filter(DagRun.run_id == run.run_id).first()
                    # 如果任务仍在运行状态,则强制将其标记为失败
                    if updated_run and updated_run.state == DagRunState.RUNNING:
                        print(f"[WATCHER] run_id: {run.run_id}, 强制结束")
                        # 更新任务状态为失败
                        updated_run.state = DagRunState.FAILED
                        # 设置任务结束时间为当前UTC时间
                        updated_run.end_date = datetime.now(timezone.utc)
                        # 将更新后的任务状态合并到会话中
                        session.merge(updated_run)
                session.commit()
            
        else:
            pass

        # 触发新的DAG运行
        run_id = f'{room_id}_{sender}_{msg_id}'
        print(f"[WATCHER] 触发AI聊天DAG，run_id: {run_id}")
        trigger_dag(dag_id='zacks_ai_agent', conf=message_data, run_id=run_id)

    else:
        # 非文字消息，暂不触发AI聊天流程
        print("[WATCHER] 不触发AI聊天流程")


# 创建DAG
dag = DAG(
    dag_id='wx_msg_watcher',
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
    max_active_runs=30,
    catchup=False,
    tags=['WCF-微信消息监控'],
    description='WCF-微信消息监控',
)

# 创建处理消息的任务
process_message = PythonOperator(
    task_id='process_wx_message',
    python_callable=process_wx_message,
    provide_context=True,
    dag=dag
)

# 设置任务依赖关系（当前只有一个任务，所以不需要设置依赖）
process_message
