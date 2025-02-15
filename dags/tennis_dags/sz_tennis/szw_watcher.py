#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Time    : 2024/3/20
@Author  : claude89757
@File    : szw_watcher.py
@Software: PyCharm
"""
import time
import datetime
import requests
import random
import concurrent.futures
from typing import List, Tuple
import uuid
import ssl
import urllib3
from urllib3.util.ssl_ import create_urllib3_context
from urllib3.poolmanager import PoolManager

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.models import Variable
from datetime import timedelta

from utils.wechat_channl import send_wx_msg

# DAG的默认参数
default_args = {
    'owner': 'claude89757',
    'depends_on_past': False,
    'start_date': datetime.datetime(2024, 1, 1),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

def print_with_timestamp(*args, **kwargs):
    """打印函数带上当前时间戳"""
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]", time.localtime())
    print(timestamp, *args, **kwargs)

def find_available_slots(booked_slots: List[List[str]], time_range: dict) -> List[List[str]]:
    """查找可用的时间段"""
    if not booked_slots:
        return [[time_range['start_time'], time_range['end_time']]]
    
    # 将时间转换为分钟
    booked_in_minutes = sorted([(int(start[:2]) * 60 + int(start[3:]), 
                                int(end[:2]) * 60 + int(end[3:]))
                               for start, end in booked_slots])
    
    start_minutes = int(time_range['start_time'][:2]) * 60 + int(time_range['start_time'][3:])
    end_minutes = int(time_range['end_time'][:2]) * 60 + int(time_range['end_time'][3:])
    
    available = []
    current = start_minutes
    
    for booked_start, booked_end in booked_in_minutes:
        if current < booked_start:
            available.append([
                f"{current // 60:02d}:{current % 60:02d}",
                f"{booked_start // 60:02d}:{booked_start % 60:02d}"
            ])
        current = max(current, booked_end)
    
    if current < end_minutes:
        available.append([
            f"{current // 60:02d}:{current % 60:02d}",
            f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
        ])
    
    return available

def get_legacy_session():
    class CustomHttpAdapter(requests.adapters.HTTPAdapter):
        def __init__(self, *args, **kwargs):
            self.poolmanager = None
            super().__init__(*args, **kwargs)

        def init_poolmanager(self, connections, maxsize, block=False):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_ciphers('DEFAULT@SECLEVEL=1')
            ctx.options |= 0x4  # 启用legacy renegotiation
            self.poolmanager = PoolManager(
                num_pools=connections,
                maxsize=maxsize,
                block=block,
                ssl_context=ctx
            )
    
    session = requests.Session()
    adapter = CustomHttpAdapter()
    session.mount('https://', adapter)
    return session

def get_free_tennis_court_infos_for_szw(date: str, proxy_list: list, time_range: dict) -> dict:
    """
    获取可预订的场地信息
    Args:
        date: 日期，格式为YYYY-MM-DD
        proxy_list: 代理列表
        time_range: 时间范围，格式为{"start_time": "HH:MM", "end_time": "HH:MM"}
    Returns:
        dict: 场地信息，格式为{场地名: [[开始时间, 结束时间], ...]}
    """
    szw_cookie = "HT.LoginType.1=20; HT.App.Type.1=10; HT.Weixin.ServiceType.1=30; HT.Weixin.AppID.1=wx6b10d95e92283e1c; HT.Weixin.OpenID.1=oH5RL5EWB5CjAPKVPOOLlfHm1bV8; HT.EmpID.1=4d5adfce-e849-48d5-b7fb-863cdf34bea0; HT.IsTrainer.1=False; HT.PartID.1=b700c053-71f2-47a6-88a1-6cf50b7cf863; HT.PartDisplayName.1=%e6%b7%b1%e5%9c%b3%e6%b9%be%e4%bd%93%e8%82%b2%e4%b8%ad%e5%bf%83; HT.ShopID.1=4f195d33-de51-495e-a345-09b23f98ce95; HT.ShopDisplayName.1=%e6%b7%b1%e5%9c%b3%e6%b9%be%e5%b0%8f%e7%a8%8b%e5%ba%8f; ASP.NET_SessionId=kzkp4in0ixh5b2ja15ntd5lt; .AspNet.ApplicationCookie=-M7ZlgO39kFB3HKUrbDJ2Xr5BLfSWTo_Ro2VRQf_Pv_g1X50ZymQcwKI4CmPMFDgTdi5e-N0IaORjtRSLVQ1uVoQ9DETv4uMYybiB18vLSEVZ4hlMd8gxdIjjGeupP3HuGFF0dOTvj2zFS1b0dm6EcKMEoQZv7t3dDJ5jsGn71WSI4lB2uGP8tqwwcLVlaAnKvAGf73dCd1uRaUvBawCpy7FcSZyPR4b_UlKGe5UxJgWuaQLseMyxpKriwalXFe4T3ZUcNwOS6bRB0mqSbKWPrgOFiIq0_WRdMAhqSNqg-cvYE7hSI4gFTRCtn_3v6em9kp4RNQqOdz-c50pk1d589Vb7ftvH0tPry8-rLM9yf0p24fR0DL8MKyi-rXTiQ8HTPTdcWrWdL30DtBRNQ4Zye8DA68RA5bV5Y61yWAf51S3s1GvVUsJ1MkBk6dPtsfkWmhG4C7Mx6-MRrMXAzrZZXrE1jB1a7wJIdSziREVZxiaQPcNcYQ5ZvFWnmtAcO_4h50NC714pIFiBdqWJbburJPof87xF6UVyZQcE3t9jqcFFUEBBZpQTiq0wi4Ejmh6CFcE9RqhaG1AUr5U6BV4Q7h3NEE3AjOtHcCx6lz-nlv0wIxs; __RequestVerificationToken_L3N6YmF50=iUyHTVkkRK6DfoP3plsDGxV7nOwcQ-xMWOh-EeW2gT6ZHPr6D4nGhrFl1d_ZRVZ3dkxSuZtREHtzL8WKiTIxYPpVA6Q1; XSRF-TOKEN=ntutMqRb0WugZfy7xPzohrV_9ye2tSviscG8iXdqwJ8Wv63Fic7N3NZNHw9gSKOd8g5wfvq3uS2xdUGlMGqit0-RqZWn1Yb2z4eBrLXUbGMlYXxaBL-Bt8rwbMH7D0jdzVYdeQ2"
    got_response = False
    response = None
    
    session = get_legacy_session()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # 禁用不安全请求警告
    
    for proxy in proxy_list:
        data = {
            'VenueTypeID': 'd3bc78ba-0d9c-4996-9ac5-5a792324decb',
            'VenueTypeDisplayName': '',
            # 'IsGetPrice': 'true',
            # 'isApp': 'true',
            'billDay': {date},
            # 'webApiUniqueID': '811e68e7-f189-675c-228d-3739e48e57b2'
        }
        headers = {
            "Host": "program.springcocoon.com",
            "sec-ch-ua": "\" Not A;Brand\";v=\"99\", \"Chromium\";v=\"98\"",
            # "X-XSRF-TOKEN": "rs3gB5gQUqeG-YcaRXgB13JMlQAn9N4e_vS29wl-_HV5-MZb6gCL7eLhiC030tJP-cFa0c2qgK9UfSKuwLH5vhZK_
            # 2KYA_j7Df_NAn9ts9q3N0A9XIJe7vAXdhZLTaywn0VRMA2",
            "sec-ch-ua-mobile": "?0",
            # "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)
            # Chrome/98.0.4758.102 Safari/537.36 MicroMessenger/6.8.0(0x16080000) NetType/WIFI MiniProgramEnv
            # /Mac MacWechat/WMPF XWEB/30803",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua-platform": "\"macOS\"",
            "Origin": "https://program.springcocoon.com",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://program.springcocoon.com/szbay/AppVenue/VenueBill/"
                       "VenueBill?VenueTypeID=d3bc78ba-0d9c-4996-9ac5-5a792324decb",
            "Accept-Language": "zh-CN,zh",
            "Cookie": szw_cookie
        }
        url = 'https://program.springcocoon.com/szbay/api/services/app/VenueBill/GetVenueBillDataAsync'
        
        print(f"trying for {proxy}")
        try:
            print(f"data: {data}")
            print(f"headers: {headers}" )
            response = session.post(url, headers=headers, data=data, timeout=15, verify=False)
            print(f"response: {response.text}")
            if response.status_code == 200:
                try:
                    print(response.json())
                except Exception as e:
                    print(f"error: {e}")
                    continue
                got_response = True
                time.sleep(1)
                break
            else:
                print(f"failed for {proxy}: {response.text}")
                continue
        except Exception as error:
            print(f"failed for {proxy}: {error}")
            continue

    if got_response and response:
        if response.status_code == 200:
            if len(response.json()['result']) == 1:
                # 场地名称转换
                venue_name_infos = {}
                for venue in response.json()['result'][0]['listVenue']:
                    venue_name_infos[venue['id']] = venue['displayName']
                print(venue_name_infos)

                booked_court_infos = {}
                if response.json()['result'][0].get("listWebVenueStatus") \
                        and response.json()['result'][0]['listWebVenueStatus']:
                    for venue_info in response.json()['result'][0]['listWebVenueStatus']:
                        if str(venue_info.get('bookLinker')) == '可定' or str(venue_info.get('bookLinker')) == '可订':
                            pass
                        else:
                            start_time = str(venue_info['timeStartEndName']).split('-')[0].replace(":30", ":00")
                            end_time = str(venue_info['timeStartEndName']).split('-')[1].replace(":30", ":00")
                            venue_name = venue_name_infos[venue_info['venueID']]
                            if booked_court_infos.get(venue_name):
                                booked_court_infos[venue_name].append([start_time, end_time])
                            else:
                                booked_court_infos[venue_name] = [[start_time, end_time]]
                else:
                    if response.json()['result'][0].get("listWeixinVenueStatus") and \
                            response.json()['result'][0]['listWeixinVenueStatus']:
                        for venue_info in response.json()['result'][0]['listWeixinVenueStatus']:
                            if venue_info['status'] == 20:
                                start_time = str(venue_info['timeStartEndName']).split('-')[0].replace(":30", ":00")
                                end_time = str(venue_info['timeStartEndName']).split('-')[1].replace(":30", ":00")
                                venue_name = venue_name_infos[venue_info['venueID']]
                                if booked_court_infos.get(venue_name):
                                    booked_court_infos[venue_name].append([start_time, end_time])
                                else:
                                    booked_court_infos[venue_name] = [[start_time, end_time]]
                            else:
                                pass
                    else:
                        pass

                available_slots_infos = {}
                for venue_id, booked_slots in booked_court_infos.items():
                    available_slots = find_available_slots(booked_slots, time_range)
                    available_slots_infos[venue_id] = available_slots
                return available_slots_infos
            else:
                raise Exception(response.text)
        else:
            raise Exception(response.text)
    else:
        raise Exception("all proxies failed")

def test_proxy(proxy: str, session: requests.Session, timeout: int = 10) -> bool:
    """
    测试代理是否可用
    Args:
        proxy: 代理地址
        session: 请求会话
        timeout: 超时时间（秒）
    Returns:
        bool: 代理是否可用
    """
    test_url = "https://program.springcocoon.com"
    try:
        response = session.get(
            test_url,
            proxies={"https": proxy},
            timeout=timeout,
            verify=False
        )
        return response.status_code == 200
    except Exception as e:
        print(f"代理 {proxy} 测试失败: {str(e)}")
        return False

def filter_available_proxies(proxy_list: List[str], max_workers: int = 10) -> List[str]:
    """
    并发测试并筛选可用的代理
    Args:
        proxy_list: 代理列表
        max_workers: 最大并发数
    Returns:
        List[str]: 可用的代理列表
    """
    session = get_legacy_session()
    available_proxies = []
    
    print_with_timestamp(f"开始测试 {len(proxy_list)} 个代理...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_proxy = {
            executor.submit(test_proxy, proxy, session): proxy 
            for proxy in proxy_list
        }
        
        for future in concurrent.futures.as_completed(future_to_proxy):
            proxy = future_to_proxy[future]
            try:
                if future.result():
                    available_proxies.append(proxy)
                    print_with_timestamp(f"代理 {proxy} 可用")
            except Exception as e:
                print_with_timestamp(f"测试代理 {proxy} 时发生错误: {str(e)}")
    
    print_with_timestamp(f"测试完成，共找到 {len(available_proxies)} 个可用代理")
    return available_proxies

def check_tennis_courts():
    """主要检查逻辑"""
    # if datetime.time(0, 0) <= datetime.datetime.now().time() < datetime.time(8, 0):
    #     print("每天0点-8点不巡检")
    #     return
    
    run_start_time = time.time()
    print_with_timestamp("start to check...")

    # 获取系统代理
    system_proxy = Variable.get("PROXY_URL", default_var="")
    if system_proxy:
        proxies = {"https": system_proxy}
    else:
        proxies = None

    # 获取代理列表
    url = "https://raw.githubusercontent.com/claude89757/free_https_proxies/main/https_proxies.txt"
    response = requests.get(url, proxies=proxies)
    text = response.text.strip()
    proxy_list = [line.strip() for line in text.split("\n")]    
    random.shuffle(proxy_list)
    
    # 测试并筛选可用代理
    available_proxies = filter_available_proxies(proxy_list)
    if not available_proxies:
        print_with_timestamp("没有找到可用的代理，退出检查")
        return
    
    print_with_timestamp(f"可用代理列表: {available_proxies}")

    # 设置查询时间范围
    time_range = {
        "start_time": "08:00",
        "end_time": "22:00"
    }

    # 使用可用代理查询空闲的球场信息
    up_for_send_data_list = []
    
    for index in range(0, 7):
        input_date = (datetime.datetime.now() + datetime.timedelta(days=index)).strftime('%Y-%m-%d')
        inform_date = (datetime.datetime.now() + datetime.timedelta(days=index)).strftime('%m-%d')
        
        try:
            court_data = get_free_tennis_court_infos_for_szw(input_date, available_proxies, time_range)
            print(f"court_data: {court_data}")
            time.sleep(1)
            
            for court_name, free_slots in court_data.items():
                if free_slots:
                    filtered_slots = []
                    check_date = datetime.datetime.strptime(input_date, '%Y-%m-%d')
                    is_weekend = check_date.weekday() >= 5
                    
                    for slot in free_slots:
                        hour_num = int(slot[0].split(':')[0])
                        if is_weekend:
                            if 15 <= hour_num <= 21:  # 周末关注15点到21点的场地
                                filtered_slots.append(slot)
                        else:
                            if 18 <= hour_num <= 21:  # 工作日仍然只关注18点到21点的场地
                                filtered_slots.append(slot)
                    
                    if filtered_slots:
                        up_for_send_data_list.append({
                            "date": inform_date,
                            "court_name": f"深圳湾{court_name}",
                            "free_slot_list": filtered_slots
                        })
        except Exception as e:
            print(f"Error checking date {input_date}: {str(e)}")
            continue

    # 处理通知逻辑
    if up_for_send_data_list:
        cache_key = "深圳湾网球场"
        sended_msg_list = Variable.get(cache_key, deserialize_json=True, default_var=[])
        up_for_send_msg_list = []
        for data in up_for_send_data_list:
            date = data['date']
            court_name = data['court_name']
            free_slot_list = data['free_slot_list']
            
            date_obj = datetime.datetime.strptime(f"{datetime.datetime.now().year}-{date}", "%Y-%m-%d")
            weekday = date_obj.weekday()
            weekday_str = ["一", "二", "三", "四", "五", "六", "日"][weekday]
            
            for free_slot in free_slot_list:
                notification = f"【{court_name}】星期{weekday_str}({date})空场: {free_slot[0]}-{free_slot[1]}"
                if notification not in sended_msg_list:
                    up_for_send_msg_list.append(notification)

        # 获取微信发送配置
        wcf_ip = Variable.get("WCF_IP", default_var="")
        for msg in up_for_send_msg_list:
            send_wx_msg(
                wcf_ip=wcf_ip,
                message=msg,
                receiver="38763452635@chatroom",
                aters=''
            )
            sended_msg_list.append(msg)

        # 更新Variable
        description = f"深圳湾网球场场地通知 - 最后更新: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        Variable.set(
            key=cache_key,
            value=sended_msg_list[-10:],
            description=description,
            serialize_json=True
        )
        print(f"updated {cache_key} with {sended_msg_list}")

    run_end_time = time.time()
    execution_time = run_end_time - run_start_time
    print_with_timestamp(f"Total cost time：{execution_time} s")

# 创建DAG
dag = DAG(
    '深圳湾网球场巡检',
    default_args=default_args,
    description='深圳湾网球场巡检',
    schedule_interval='*/10 * * * *',  # 每5分钟执行一次
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=3),
    catchup=False,
    tags=['深圳']
)

# 创建任务
check_courts_task = PythonOperator(
    task_id='check_tennis_courts',
    python_callable=check_tennis_courts,
    dag=dag,
)

# 设置任务依赖关系
check_courts_task


# # 测试
# if __name__ == "__main__":
#     data = get_free_tennis_court_infos_for_szw("2025-02-15", ["34.215.74.117:1080"], {"start_time": "08:00", "end_time": "22:00"})
#     for court_name, free_slots in data.items():
#         print(f"{court_name}: {free_slots}")
