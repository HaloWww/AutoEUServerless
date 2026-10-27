# SPDX-License-Identifier: GPL-3.0-or-later

"""
euserv 自动续期脚本
功能:
* 使用 TrueCaptcha API 自动识别验证码
* 发送通知到 Telegram
* 增加登录失败重试机制
* 日志信息格式化
"""

import os
import re
import sys
import json
import time
import base64
import requests
from bs4 import BeautifulSoup

# 账户信息：用户名和密码
USERNAME = os.getenv('EUSERV_USERNAME')  # 填写用户名或邮箱
PASSWORD = os.getenv('EUSERV_PASSWORD')  # 填写密码

# TrueCaptcha API 配置
TRUECAPTCHA_USERID = os.getenv('TRUECAPTCHA_USERID')
TRUECAPTCHA_APIKEY = os.getenv('TRUECAPTCHA_APIKEY')

# Mailparser 配置
MAILPARSER_DOWNLOAD_URL_ID = os.getenv('MAILPARSER_DOWNLOAD_URL_ID')
MAILPARSER_DOWNLOAD_BASE_URL = "https://files.mailparser.io/d/"

# Telegram Bot 推送配置
TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN')
TG_USER_ID = os.getenv('TG_USER_ID')
TG_API_HOST = "https://api.telegram.org"

# 代理设置（如果需要）
PROXIES = {"http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"}

# 最大登录重试次数
LOGIN_MAX_RETRY_COUNT = 5

# 接收 PIN 的等待时间，单位为秒
WAITING_TIME_OF_PIN = 15
PIN_MAX_WAIT_TIME = 180
REQUEST_TIMEOUT = (10, 30)

# 是否检查验证码解决器的使用情况
CHECK_CAPTCHA_SOLVER_USAGE = True

user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/95.0.4638.69 Safari/537.36"
)
desp = ""  # 日志信息

def log(info: str):
    # 打印并记录日志信息，附带 emoji 以增加可读性
    emoji_map = {
        "正在续费": "🔄",
        "检测到": "🔍",
        "ServerID": "🔗",
        "无需更新": "✅",
        "续订错误": "⚠️",
        "已成功续订": "🎉",
        "所有工作完成": "🏁",
        "登陆失败": "❗",
        "验证通过": "✔️",
        "验证失败": "❌",
        "API 使用次数": "📊",
        "验证码是": "🔢",
        "登录尝试": "🔑",
        "[MailParser]": "📧",
        "[Captcha Solver]": "🧩",
        "[AutoEUServerless]": "🌐",
    }
    # 对每个关键字进行检查，并在找到时添加 emoji
    for key, emoji in emoji_map.items():
        if key in info:
            info = emoji + " " + info
            break

    print(info, flush=True)
    global desp
    desp += info + "\n\n"


# 登录重试装饰器
def login_retry(*args, **kwargs):
    def wrapper(func):
        def inner(username, password):
            ret, ret_session = func(username, password)
            max_retry = kwargs.get("max_retry")
            # 默认重试 3 次
            if not max_retry:
                max_retry = 3
            number = 0
            if ret == "-1":
                while number < max_retry:
                    number += 1
                    if number > 1:
                        log("[AutoEUServerless] 登录尝试第 {} 次".format(number))
                    sess_id, session = func(username, password)
                    if sess_id != "-1":
                        return sess_id, session
                    else:
                        if number == max_retry:
                            return sess_id, session
            else:
                return ret, ret_session
        return inner
    return wrapper

# 验证码解决器
def captcha_solver(captcha_image_url: str, session: requests.session) -> dict:
    # TrueCaptcha API 文档: https://apitruecaptcha.org/api
    # 每天免费使用 100 次请求。

    response = session.get(captcha_image_url)
    encoded_string = base64.b64encode(response.content)
    url = "https://api.apitruecaptcha.org/one/gettext"

    data = {
        "userid": TRUECAPTCHA_USERID,
        "apikey": TRUECAPTCHA_APIKEY,
        "case": "mixed",
        "mode": "human",
        "data": str(encoded_string)[2:-1],
    }
    r = requests.post(url=url, json=data)
    j = json.loads(r.text)
    return j

# 处理验证码解决结果
def handle_captcha_solved_result(solved: dict) -> str:
    # 处理验证码解决结果# 
    if "result" in solved:
        solved_text = solved["result"]
        if "RESULT  IS" in solved_text:
            log("[Captcha Solver] 使用的是演示 apikey。")
            # 因为使用了演示 apikey
            text = re.findall(r"RESULT  IS . (.*) .", solved_text)[0]
        else:
            # 使用自己的 apikey
            log("[Captcha Solver] 使用的是您自己的 apikey。")
            text = solved_text
        operators = ["X", "x", "+", "-"]
        if any(x in text for x in operators):
            for operator in operators:
                operator_pos = text.find(operator)
                if operator == "x" or operator == "X":
                    operator = "*"
                if operator_pos != -1:
                    left_part = text[:operator_pos]
                    right_part = text[operator_pos + 1 :]
                    if left_part.isdigit() and right_part.isdigit():
                        return eval(
                            "{left} {operator} {right}".format(
                                left=left_part, operator=operator, right=right_part
                            )
                        )
                    else:
                        # 这些符号("X", "x", "+", "-")不会同时出现，
                        # 它只包含一个算术符号。
                        return text
        else:
            return text
    else:
        print(solved)
        raise KeyError("未找到解析结果。")

# 获取验证码解决器使用情况
def get_captcha_solver_usage() -> dict:
    # 获取验证码解决器的使用情况# 
    url = "https://api.apitruecaptcha.org/one/getusage"

    params = {
        "username": TRUECAPTCHA_USERID,
        "apikey": TRUECAPTCHA_APIKEY,
    }
    r = requests.get(url=url, params=params)
    j = json.loads(r.text)
    return j

# 从 Mailparser 获取 PIN
def get_mailparser_records(url_id: str) -> list:
    response = requests.get(
        f"{MAILPARSER_DOWNLOAD_BASE_URL}{url_id}",
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError("Mailparser 下载失败（HTTP {}），请检查下载地址及解析额度".format(response.status_code))
    try:
        records = response.json()
    except ValueError:
        raise RuntimeError("Mailparser 未返回 JSON，请检查下载格式及解析额度") from None
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise RuntimeError("Mailparser 返回格式错误，请检查下载地址及解析额度")
    return records


def mailparser_record_id(record: dict) -> str:
    # 使用整条记录区分邮件；新的邮件即使碰巧 PIN 相同也可以识别。
    return json.dumps(record, sort_keys=True, ensure_ascii=False)


def get_pin_from_mailparser(url_id: str, previous_records: set) -> str:
    deadline = time.monotonic() + PIN_MAX_WAIT_TIME
    while True:
        for record in get_mailparser_records(url_id):
            if mailparser_record_id(record) in previous_records:
                continue
            pin = str(record.get("pin") or "").strip()
            if pin:
                log("[MailParser] 已获取本次请求的新 PIN")
                return pin
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Mailparser 未收到本次请求的新 PIN；请检查邮件转发、解析规则及额度")
        time.sleep(min(WAITING_TIME_OF_PIN, remaining))

# 登录函数
@login_retry(max_retry=LOGIN_MAX_RETRY_COUNT)
def login(username: str, password: str) -> (str, requests.session):
    # 登录 EUserv 并获取 session# 
    headers = {"user-agent": user_agent, "origin": "https://www.euserv.com"}
    url = "https://support.euserv.com/index.iphp"
    captcha_image_url = "https://support.euserv.com/securimage_show.php"
    session = requests.Session()

    sess = session.get(url, headers=headers)
    sess_id = re.findall("PHPSESSID=(\\w{10,100});", str(sess.headers))[0]
    session.get("https://support.euserv.com/pic/logo_small.png", headers=headers)

    login_data = {
        "email": username,
        "password": password,
        "form_selected_language": "en",
        "Submit": "Login",
        "subaction": "login",
        "sess_id": sess_id,
    }
    f = session.post(url, headers=headers, data=login_data)
    f.raise_for_status()

    if "Hello" not in f.text and "Confirm or change your customer data here" not in f.text:
        if "To finish the login process please solve the following captcha." not in f.text:
            return "-1", session
        else:
            log("[Captcha Solver] 正在进行验证码识别...")
            solved_result = captcha_solver(captcha_image_url, session)
            captcha_code = handle_captcha_solved_result(solved_result)
            log("[Captcha Solver] 识别的验证码是: {}".format(captcha_code))

            if CHECK_CAPTCHA_SOLVER_USAGE:
                usage = get_captcha_solver_usage()
                log("[Captcha Solver] 当前日期 {0} API 使用次数: {1}".format(
                    usage[0]["date"], usage[0]["count"]
                ))

            f2 = session.post(
                url,
                headers=headers,
                data={
                    "subaction": "login",
                    "sess_id": sess_id,
                    "captcha_code": captcha_code,
                },
            )
            if "To finish the login process please solve the following captcha." not in f2.text:
                log("[Captcha Solver] 验证通过")
                return sess_id, session
            else:
                log("[Captcha Solver] 验证失败")
                return "-1", session
    else:
        return sess_id, session

# 获取服务器列表
def get_servers(sess_id: str, session: requests.session) -> {}:
    # 获取服务器列表# 
    d = {}
    url = "https://support.euserv.com/index.iphp"
    headers = {"user-agent": user_agent, "origin": "https://www.euserv.com"}
    # 续期后会停留在合同详情页，必须显式返回总览才能再次获取服务器列表。
    f = session.get(url=url, headers=headers,
                    params={"sess_id": sess_id, "action": "show_default"},
                    timeout=REQUEST_TIMEOUT)
    f.raise_for_status()
    soup = BeautifulSoup(f.text, "html.parser")
    for tr in soup.select(
        "#kc2_order_customer_orders_tab_content_1 .kc2_order_table.kc2_content_table tr"
    ):
        server_id = tr.select(".td-z1-sp1-kc")
        if not len(server_id) == 1:
            continue
        actions = tr.select(".td-z1-sp2-kc .kc2_order_action_container")
        if not actions:
            raise RuntimeError("服务器列表缺少续期状态，无法确认是否需要续期")
        flag = (
            True
            if actions[0]
            .get_text()
            .find("Contract extension possible from")
            == -1
            else False
        )
        d[server_id[0].get_text(strip=True)] = flag
    if not d:
        if "Confirm or change your customer data here" in f.text:
            raise RuntimeError("登录后需要确认客户信息，未获取到服务器列表")
        raise RuntimeError("未获取到服务器列表，可能登录失效或页面发生变化")
    return d

# 续期操作
def renew(
    sess_id: str, session: requests.session, password: str, order_id: str, mailparser_dl_url_id: str
) -> bool:
    # 执行续期操作# 
    url = "https://support.euserv.com/index.iphp"
    headers = {
        "user-agent": user_agent,
        "Host": "support.euserv.com",
        "origin": "https://support.euserv.com",
        "Referer": "https://support.euserv.com/index.iphp",
    }
    data = {
        "Submit": "Extend contract",
        "sess_id": sess_id,
        "ord_no": order_id,
        "subaction": "choose_order",
        "choose_order_subaction": "show_contract_details",
    }
    response = session.post(url, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    previous_records = {
        mailparser_record_id(row) for row in get_mailparser_records(mailparser_dl_url_id)
    }

    # 弹出 'Security Check' 窗口，将自动触发 '发送 PIN'。
    response = session.post(
        url,
        headers=headers,
        data={
            "sess_id": sess_id,
            "subaction": "show_kc2_security_password_dialog",
            "prefix": "kc2_customer_contract_details_extend_contract_",
            "type": "1",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    # 等待邮件解析器解析出 PIN
    pin = get_pin_from_mailparser(mailparser_dl_url_id, previous_records)

    # 使用 PIN 获取 token
    data = {
        "auth": pin,
        "sess_id": sess_id,
        "subaction": "kc2_security_password_get_token",
        "prefix": "kc2_customer_contract_details_extend_contract_",
        "type": 1,
        "ident": f"kc2_customer_contract_details_extend_contract_{order_id}",
    }
    f = session.post(url, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
    f.raise_for_status()
    result = f.json()
    if result.get("rs") != "success":
        log("[AutoEUServerless] 续期 PIN 验证失败，未取得续期令牌")
        return False
    token = result["token"]["value"]
    data = {
        "sess_id": sess_id,
        "ord_id": order_id,
        "subaction": "kc2_customer_contract_details_extend_contract_term",
        "token": token,
    }
    response = session.post(url, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    time.sleep(5)
    servers = get_servers(sess_id, session)
    return order_id in servers and not servers[order_id]

# 检查续期状态
def check(sess_id: str, session: requests.session, expected_orders=None):
    # 检查续期状态# 
    print("Checking.......")
    d = get_servers(sess_id, session)
    if expected_orders and not set(expected_orders).issubset(d):
        raise RuntimeError("续期检查时服务器列表不完整")
    flag = True
    for key, val in d.items():
        if val:
            flag = False
            log("[AutoEUServerless] ServerID: %s 续期失败!" % key)

    return flag

# 发送 Telegram 通知
def telegram():
    message = (
        "<b>AutoEUServerless 日志</b>\n\n" + desp +
        "\n<b>版权声明：</b>\n"
        "本脚本基于 GPL-3.0 许可协议，版权所有。\n\n"
        
        "<b>致谢：</b>\n"
        "特别感谢 <a href='https://github.com/lw9726/eu_ex'>eu_ex</a> 的贡献和启发, 本项目在此基础整理。\n"
        "开发者：<a href='https://github.com/lw9726/eu_ex'>WizisCool</a>\n"
        "<a href='https://www.nodeseek.com/space/8902#/general'>个人Nodeseek主页</a>\n"
        "<a href='https://dooo.ng'>个人小站Dooo.ng</a>\n\n"
        "<b>支持项目：</b>\n"
        "⭐️ 给我们一个 GitHub Star! ⭐️\n"
        "<a href='https://github.com/WizisCool/AutoEUServerless'>访问 GitHub 项目</a>"
    )

    # 请不要删除本段版权声明, 开发不易, 感谢! 感谢!
    # 请勿二次售卖,出售,开源不易,万分感谢!
    data = {
        "chat_id": TG_USER_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    }
    response = requests.post(
        TG_API_HOST + "/bot" + TG_BOT_TOKEN + "/sendMessage", data=data,
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        print("Telegram Bot 推送失败")
    else:
        print("Telegram Bot 推送成功")



def main_handler(event, context):
    # 主函数，处理每个账户的续期# 
    if not USERNAME or not PASSWORD:
        log("[AutoEUServerless] 你没有添加任何账户")
        exit(1)
    user_list = USERNAME.strip().split()
    passwd_list = PASSWORD.strip().split()
    mailparser_dl_url_id_list = MAILPARSER_DOWNLOAD_URL_ID.strip().split()
    if len(user_list) != len(passwd_list):
        log("[AutoEUServerless] 用户名和密码数量不匹配!")
        exit(1)
    if len(mailparser_dl_url_id_list) != len(user_list):
        log("[AutoEUServerless] mailparser_dl_url_ids 和用户名的数量不匹配!")
        exit(1)
    failed = False
    for i in range(len(user_list)):
        print("*" * 30)
        log("[AutoEUServerless] 正在续费第 %d 个账号" % (i + 1))
        s = None
        try:
            sessid, s = login(user_list[i], passwd_list[i])
            if sessid == "-1":
                failed = True
                log("[AutoEUServerless] 第 %d 个账号登陆失败，请检查登录信息" % (i + 1))
                continue
            servers = get_servers(sessid, s)
            log("[AutoEUServerless] 检测到第 {} 个账号有 {} 台 VPS，正在尝试续期".format(i + 1, len(servers)))
            for k, v in servers.items():
                if v:
                    if not renew(sessid, s, passwd_list[i], k, mailparser_dl_url_id_list[i]):
                        failed = True
                        log("[AutoEUServerless] ServerID: %s 续订错误!" % k)
                    else:
                        log("[AutoEUServerless] ServerID: %s 已成功续订!" % k)
                else:
                    log("[AutoEUServerless] ServerID: %s 无需更新" % k)
            time.sleep(15)
            if not check(sessid, s, servers):
                failed = True
        except RuntimeError as exc:
            failed = True
            log("[AutoEUServerless] 第 {} 个账号处理失败: {}".format(i + 1, exc))
        except Exception as exc:
            failed = True
            # 请求异常可能带有包含密钥的 URL，只记录异常类型。
            log("[AutoEUServerless] 第 {} 个账号处理失败: {}".format(i + 1, type(exc).__name__))
        finally:
            if s is not None:
                s.close()

    if failed:
        log("[AutoEUServerless] 续期任务失败，请检查上述错误")
    else:
        log("[AutoEUServerless] 所有工作完成！尽情享受~")

    # 发送 Telegram 通知
    if TG_BOT_TOKEN and TG_USER_ID and TG_API_HOST:
        try:
            telegram()
        except requests.RequestException:
            log("[AutoEUServerless] Telegram Bot 推送失败")

    print("*" * 30)
    return not failed

if __name__ == "__main__":
    sys.exit(0 if main_handler(None, None) else 1)
