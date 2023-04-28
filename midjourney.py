#!/usr/bin/env python
# -*- coding=utf-8 -*-
"""
@time: 2023/4/25 11:46
@Project ：chatgpt-on-wechat
@file: midjourney.py
"""
import json
import os
import random
import string
import time
import unicodedata
import requests
import oss2
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from bridge.bridge import Bridge
from config import conf
import plugins
from plugins import *
from common.log import logger
from common.expired_dict import ExpiredDict


def is_chinese(prompt):
    for char in prompt:
        if "CJK" in unicodedata.name(char):
            return True
    return False


@plugins.register(name="Midjourney", desc="用midjourney api来画图", desire_priority=1, version="0.1", author="ffwen123")
class Midjourney(Plugin):
    def __init__(self):
        super().__init__()
        curdir = os.path.dirname(__file__)
        config_path = os.path.join(curdir, "config.json")
        self.params_cache = ExpiredDict(60 * 60)
        if not os.path.exists(config_path):
            logger.info('[RP] 配置文件不存在，将使用config.json.template模板')
            config_path = os.path.join(curdir, "config.json.template")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                self.api_url = config["api_url"]
                self.call_back_url = config["call_back_url"]
                self.no_get_response = config["no_get_response"]
                self.rule = config["rule"]
                self.oss_conf = config["oss_conf"]
                auth = oss2.Auth(self.oss_conf["akid"], self.oss_conf["akst"])
                self.bucket_img = oss2.Bucket(auth, self.oss_conf["aked"], self.oss_conf["bucket_name"])
                self.headers = config["headers"]
                self.default_params = config["defaults"]
                self.slash_commands_data = config["slash_commands_data"]
                self.mj_api_key = self.headers.get("Authorization", "")
                if not self.mj_api_key or "你的API 密钥" in self.mj_api_key:
                    raise Exception("please set your Midjourney api key in config or environment variable.")
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
            logger.info("[RP] inited")
        except Exception as e:
            if isinstance(e, FileNotFoundError):
                logger.warn(f"[RP] init failed, config.json not found.")
            else:
                logger.warn("[RP] init failed." + str(e))
            raise e

    def on_handle_context(self, e_context: EventContext):

        if e_context['context'].type not in [ContextType.IMAGE_CREATE, ContextType.IMAGE]:
            return
        logger.debug("[RP] on_handle_context. content: %s" % e_context['context'].content)
        try:
            logger.info("[RP] image_test={}".format(str(e_context['context'])))
        except:
            pass
        logger.info("[RP] image_query={}".format(e_context['context'].content))
        reply = Reply()
        try:
            user_id = e_context['context']["session_id"]
            content = e_context['context'].content[:]
            if e_context['context'].type == ContextType.IMAGE_CREATE:
                # 解析用户输入 如"mj [img2img] prompt --v 5 --ar 3:2"
                if content.find("--") >= 0:
                    prompt, commands = content.split("--", 1)
                    commands = " --" + commands
                elif content.find("—") >= 0:
                    prompt, commands = content.split("—", 1)
                    commands = " —" + commands
                else:
                    prompt, commands = content, ""
                if "help" in content or "帮助" in content:
                    reply.type = ReplyType.INFO
                    reply.content = self.get_help_text(verbose=True)
                else:
                    flag = False
                    if self.rule.get("image") in prompt:
                        flag = True
                        prompt = prompt.replace(self.rule.get("image"))
                    if is_chinese(prompt):
                        prompt = Bridge().fetch_translate(prompt, to_lang="en")
                    if len(prompt) > 250:
                        prompt = prompt[:250] + commands
                    else:
                        prompt += commands
                    params = {**self.slash_commands_data}
                    if params.get("prompt", ""):
                        params["prompt"] += f", {prompt}"
                    else:
                        params["prompt"] += f"{prompt}"
                    logger.info("[RP] params={}".format(params))
                    if flag:
                        self.params_cache[user_id] = params
                        reply.type = ReplyType.INFO
                        reply.content = "请发送一张图片给我"
                    else:
                        post_json = {**self.default_params, **{
                            "cmd": self.slash_commands_data.get("cmd", "imagine"),
                            "msg": params["prompt"]
                        }}
                        logger.info("[RP] txt2img post_json={}".format(post_json))
                        # 调用midjourney api来画图
                        http_resp, messageId = self.get_imageurl(url=self.api_url, data=post_json)
                        if messageId:
                            reply.type = ReplyType.IMAGE_URL
                            reply.content = http_resp.get("imageUrl")
                        else:
                            reply.type = ReplyType.ERROR
                            reply.content = http_resp
                            e_context['reply'] = reply
                            logger.error("[RP] Midjourney API api_data: %s " % http_resp)
                    e_context.action = EventAction.BREAK_PASS  # 事件结束后，跳过处理context的默认逻辑
                    e_context['reply'] = reply
            else:
                cmsg = e_context['context']['msg']
                if user_id in self.params_cache:
                    params = self.params_cache[user_id]
                    del self.params_cache[user_id]
                    cmsg.prepare()
                    img_data = open(content, "rb")
                    rand_str = "".join(random.sample(string.ascii_letters + string.digits, 8))
                    num_str = str(random.uniform(1, 10)).split(".")[-1]
                    filename = f"{rand_str}_{num_str}" + ".png"
                    oss_imgurl = self.put_oss_image(filename, img_data)
                    if oss_imgurl:
                        post_json = {**self.default_params, **{
                            "cmd": self.slash_commands_data.get("cmd", "imagine"),
                            "msg": f'''"cmd":"{oss_imgurl} {params["prompt"]}"'''
                        }}
                        logger.info("[RP] img2img post_json={}".format(post_json))
                        # 调用midjourney api图生图
                        http_resp, messageId = self.get_imageurl(url=self.api_url, data=post_json)
                        if messageId:
                            reply.type = ReplyType.IMAGE_URL
                            reply.content = http_resp.get("imageUrl")
                        else:
                            reply.type = ReplyType.ERROR
                            reply.content = http_resp
                            e_context['reply'] = reply
                            logger.error("[RP] Midjourney API api_data: %s " % http_resp)
                    else:
                        reply.type = ReplyType.ERROR
                        reply.content = "oss上传图片失败"
                        e_context['reply'] = reply
                        logger.error("[RP] oss2 image result: oss上传图片失败")
                    e_context['reply'] = reply
                    e_context.action = EventAction.BREAK_PASS  # 事件结束后，跳过处理context的默认逻辑
        except Exception as e:
            reply.type = ReplyType.ERROR
            reply.content = "[RP] " + str(e)
            e_context['reply'] = reply
            logger.exception("[RP] exception: %s" % e)
            e_context.action = EventAction.CONTINUE

    def get_help_text(self, verbose=False, **kwargs):
        if not conf().get('image_create_prefix'):
            return "画图功能未启用"
        else:
            trigger = conf()['image_create_prefix'][0]
        help_text = "利用midjourney api来画图。\n"
        if not verbose:
            return help_text

        help_text += f"使用方法:\n使用\"{trigger}[关键词1] [关键词2]...:提示语\"的格式作画，如\"{trigger}二次元:girl\"\n"
        # help_text += "目前可用关键词：\n"
        # for rule in self.rules:
        #     keywords = [f"[{keyword}]" for keyword in rule['keywords']]
        #     help_text += f"{','.join(keywords)}"
        #     if "desc" in rule:
        #         help_text += f"-{rule['desc']}\n"
        #     else:
        #         help_text += "\n"
        return help_text

    def get_imageurl(self, url, data):
        api_data = requests.post(url=url, headers=self.headers, json=data, timeout=30.05)
        if api_data.status_code != 200:
            time.sleep(2)
            api_data = requests.post(url=self.api_url, headers=self.headers, json=data, timeout=30.05)
        if api_data.status_code == 200:
            # 调用Webhook URL的响应，来获取图片的URL
            messageId = api_data.json().get("messageId")
            logger.info("[RP] api_data={}".format(api_data.json()))
            get_resp = requests.get(url=self.call_back_url, params={"id": messageId}, timeout=30.05)
            # Webhook URL的响应慢，没隔 5 秒获取一次，超过600秒判断没有结果
            if get_resp.status_code == 200:
                if get_resp.text == self.no_get_response:
                    out_time = time.time()
                    while get_resp.text == self.no_get_response:
                        if time.time() - out_time > 600:
                            break
                        time.sleep(5)
                        get_resp = requests.get(url=self.call_back_url, params={"id": messageId}, timeout=30.05)
                logger.info("[RP] get_imageUrl={}".format(get_resp.text))
                if "imageUrl" in get_resp.text:
                    return get_resp.json(), messageId
                else:
                    return get_resp.text, None
            else:
                return "图片URL获取失败", None
        else:
            return api_data.text, None

    def put_oss_image(self, data_name, img_bytes):
        try:
            _result = self.bucket_img.put_object(self.oss_conf["image_addre"] + data_name, img_bytes)
        except Exception as e:
            print(e)
            try:
                time.sleep(3)
                _result = self.bucket_img.put_object(self.oss_conf["image_addre"] + data_name, img_bytes)
            except Exception as e:
                return None
        print("_result: ", _result)
        return self.oss_conf["image_url"].format(data_name)
