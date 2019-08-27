#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import threading
import socket
import paramiko
import selectors2 as selectors  # 基于 select 封装的多路复用 IO 库
import time
import json
from django.core.cache import cache
import django.utils.timezone as timezone
from server.models import RemoteUserBindHost
from webssh.models import TerminalLog, TerminalSession
from util.tool import gen_rand_char
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.conf import settings
import traceback
import warnings
warnings.filterwarnings("ignore")
paramiko.util.log_to_file('./paramiko.log')

# ssh_client ===>>          proxy_ssh             ==>> ssh_server
# ssh_client ===>> (proxy_server -> proxy_client) ==>> ssh_server


def terminal_log(user, hostname, ip, protocol, port, username, cmd, detail, address, useragent, start_time):
    event = TerminalLog()
    event.user = user
    event.hostname = hostname
    event.ip = ip
    event.protocol = protocol
    event.port = port
    event.username = username
    event.cmd = cmd
    event.detail = detail
    event.address = address
    event.useragent = useragent
    event.start_time = start_time
    event.save()


def transport_keepalive(transport):
    # 对后端transport每隔x秒发送空数据以保持连接
    # send_keepalive = CliSSH.get('send_keepalive', 0)
    send_keepalive = 15
    transport.set_keepalive(send_keepalive)


class ServerInterface(paramiko.ServerInterface):
    # proxy_ssh = (proxy_server + proxy_client)
    def __init__(self):
        self.event = threading.Event()
        self.tty_args = ['?', 80, 40]  # 终端参数(终端, 长, 宽)
        # self.ssh_args = None  # ssh连接参数
        self.ssh_args = None
        self.type = None
        self.http_user = None  # 终端日志 -- http用户
        self.hostname = None        # 后端主机名称
        self.password = None
        self.hostid = None  # 终端日志 -- hostid
        self.closed = False
        self.chan_cli = None
        self.client = None
        self.client_addr = None
        self.group = 'session_' + gen_rand_char()
        self.cmd = ''       # 多行命令
        self.cmd_tmp = ''   # 一行命令
        self.tab_mode = False   # 使用tab命令补全时需要读取返回数据然后添加到当前输入命令后
        self.history_mode = False
        self.res_file = gen_rand_char(16) + '.txt'
        self.start_time = time.time()
        self.log_start_time = timezone.now()
        self.last_save_time = self.start_time
        self.res_asciinema = []
        self.res_asciinema.append(
            json.dumps(
                {
                 "version": 2,
                 "width": 250,  # 设置足够宽，以便播放时全屏不至于显示错乱
                 "height": 40,
                 "timestamp": int(self.start_time),
                 "env": {"SHELL": "/bin/sh", "TERM": "linux"}
                 }
            )
        )

    def close_ssh_self(self, sleep_time=5):
        try:
            while 1:
                time.sleep(sleep_time)   # 每次循环暂停5秒，以免对 redis 造成压力
                if not cache.get('{}_{}_ssh_session'.format(self.http_user, self.password), False):
                    if not self.closed:
                        try:
                            self.chan_cli.send('\n\r系统管理员已强制中止了您的终端连接\r\n')
                        except:
                            pass
                        try:
                            self.close()
                        except:
                            pass
                    try:
                        # 发送数据给查看会话的 websocket 链接
                        message = dict()
                        message['status'] = 2
                        message['message'] = '\n\r系统管理员已强制中止了您的终端连接\r\n'
                        channel_layer = get_channel_layer()
                        async_to_sync(channel_layer.group_send)(self.group, {
                            "type": "chat.message",
                            "text": message,
                        })
                    except:
                        pass
                    break
        except:
            pass

    def conn_ssh(self):
        # proxy_client ==>> ssh_server
        proxy_client = paramiko.SSHClient()
        proxy_client.load_system_host_keys()
        proxy_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            print("*** Connecting SSH (%s@%s) ...." % (self.ssh_args[2], self.ssh_args[0]))
            proxy_client.connect(*self.ssh_args)
            self.chan_ser = proxy_client.invoke_shell(*self.tty_args)
            print("*** Connecting SSH ok")

            data = {
                'name': '{}_{}_ssh_session'.format(self.http_user, self.password),
                'group': self.group,
                'user': self.http_user,
                'host': self.ssh_args[0],
                'username': self.ssh_args[2],
                'protocol': 1,      # 1 ssh
                'port': self.ssh_args[1],
                'type': 3,      # 3 clissh
            }
            TerminalSession.objects.create(**data)

            # 设置连接到redis，使管理员可强制关闭软件终端 会话最大有效时间 30 天
            cache.set('{}_{}_ssh_session'.format(self.http_user, self.password), True, timeout=60 * 60 * 24 * 30)
            t = threading.Thread(target=self.close_ssh_self)
            t.daemon = True
            t.start()
            
            try:
                self.client = self.chan_cli.transport.remote_version
            except:
                self.client = 'clissh'
            try:
                self.client_addr = self.chan_cli.transport.sock.getpeername()[0]
            except:
                self.client_addr = '1.0.0.0'
            
        except BaseException:
            print(traceback.format_exc())
            self.close()

    def bridge(self):
        # 桥接 客户终端 和 代理服务终端 交互
        # transport_keepalive(self.chan_ser.transport)
        sel = selectors.DefaultSelector()  # Linux epol
        sel.register(self.chan_cli, selectors.EVENT_READ)
        sel.register(self.chan_ser, selectors.EVENT_READ)
        while self.chan_ser and self.chan_cli and not (self.chan_ser.closed or self.chan_cli.closed):
            events = sel.select(timeout=60)
            for key, n in events:
                if key.fileobj == self.chan_ser:
                    try:
                        recv_message = self.chan_ser.recv(1024)
                        if len(recv_message) == 0:
                            self.chan_cli.send("\r\n服务端已断开连接....\r\n")
                            time.sleep(1)
                            break
                        else:
                            try:
                                # 发送数据给查看会话的 websocket 组
                                message = dict()
                                message['status'] = 0
                                message['message'] = recv_message.decode('utf-8')
                                channel_layer = get_channel_layer()
                                async_to_sync(channel_layer.group_send)(self.group, {
                                    "type": "chat.message",
                                    "text": message,
                                })
                            except:
                                pass
                            self.chan_cli.send(recv_message)
                            # 记录操作录像
                            try:
                                """
                                防止 sz rz 传输文件时的报错
                                """
                                delay = round(time.time() - self.start_time, 6)
                                self.res_asciinema.append(json.dumps([delay, 'o', recv_message.decode('utf-8')]))
                                # 250条结果或者指定秒数就保存一次，这个任务可以优化为使用 celery
                                if len(self.res_asciinema) > 250 or int(time.time() - self.last_save_time) > 30:
                                    tmp = list(self.res_asciinema)
                                    self.res_asciinema = []
                                    self.last_save_time = time.time()
                                    with open(settings.TERMINAL_LOGS + '/' + self.res_file, 'a+') as f:
                                        for line in tmp:
                                            f.write('{}\n'.format(line))
                            except BaseException:
                                pass
                    except socket.timeout:
                        pass
                if key.fileobj == self.chan_cli:
                    try:
                        send_message = self.chan_cli.recv(1024)
                        if len(send_message) == 0:
                            print("\r\n客户端断开了连接....\r\n")
                            time.sleep(1)
                            break
                        else:
                            self.chan_ser.send(send_message)
                    except socket.timeout:
                        pass
                    except socket.error:
                        break

    def close(self):
        # 关闭ssh终端，必须分开 try 关闭，否则当强制关闭一方时，另一方连接可能被挂起
        try:
            self.chan_cli.transport.close()
        except:
            pass

        try:
            self.chan_ser.transport.close()
        except:
            pass

        try:
            if self.res_asciinema:                  
                terminal_log(
                    self.http_user,
                    self.hostname,
                    self.ssh_args[0],
                    'ssh',
                    self.ssh_args[1],
                    self.ssh_args[2],
                    # self.ssh.cmd,
                    '',
                    self.res_file,
                    self.client_addr,    # 客户端 ip
                    self.client,
                    self.log_start_time,
                )
        except:
            pass

        try:
            tmp = list(self.res_asciinema)
            self.res_asciinema = []
            with open(settings.TERMINAL_LOGS + '/' + self.res_file, 'a+') as f:
                for line in tmp:
                    f.write('{}\n'.format(line))
        except:
            pass

        try:
            TerminalSession.objects.filter(name='{}_{}_ssh_session'.format(self.http_user, self.password)).delete()
        except:
            pass

        try:
            # 发送数据给查看会话的 websocket 链接
            message = dict()
            message['status'] = 1
            message['message'] = '\n\r连接已断开\r\n'
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(self.group, {
                "type": "chat.message",
                "text": message,
            })
        except:
            pass

        try:
            cache.delete('{}_{}_ssh_session'.format(self.http_user, self.password))
        except:
            pass

        if not self.closed:
            print('SSH ({0[2]}@{0[0]}) end..................'.format(self.ssh_args))
            self.closed = True

    def set_ssh_args(self, hostid):
        # 准备proxy_client ==>> ssh_server连接参数，用于后续SSH、SFTP
        remote_host = RemoteUserBindHost.objects.get(id=hostid)
        self.hostname = remote_host.hostname
        host = remote_host.ip
        port = remote_host.port
        user = remote_host.remote_user.username
        passwd = remote_host.remote_user.password
        # self.ssh_args = ('192.168.223.112', 22, 'root', '123456')
        self.ssh_args = (host, port, user, passwd)

    def check_channel_request(self, kind, chanid):
        """
        securecrt 和 xshell 会话克隆功能（包括 securecrt 的 sftp session）会在
        同一个socket连接下（transport）开启多个channel，第一个channel id 为 0 后面 +1 递增
        由于 paramiko 实现的 ssh server 在克隆会话后，被克隆的会话就无法操作了，解决方法还没研究出来，
        所以这里使用 and chanid is 0 禁止克隆会话（开启多个 channel）
        """
        if kind == "session" and chanid is 0:
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_password(self, http_user, password):
        # 验证密码
        try:
            self.http_user = http_user
            self.password = password
            return paramiko.AUTH_SUCCESSFUL
        except BaseException:
            return paramiko.AUTH_FAILED

    def check_auth_gssapi_keyex(
        self, username, gss_authenticated=paramiko.AUTH_FAILED, cc_file=None
    ):
        if gss_authenticated == paramiko.AUTH_SUCCESSFUL:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def enable_auth_gssapi(self):
        return True

    def get_allowed_auths(self, username):
        return "gssapi-keyex,gssapi-with-mic,password,publickey"

    def check_channel_shell_request(self, channel):
        self.event.set()
        return True

    def check_channel_pty_request(
        self, channel, term, width, height, pixelwidth, pixelheight, modes
    ):
        key = 'ssh_%s_%s' % (self.http_user, self.password)
        key_ssh = 'ssh_%s_%s_ssh_count' % (self.http_user, self.password)
        key_sftp = 'ssh_%s_%s_sftp_count' % (self.http_user, self.password)
        try:
            ssh_count = cache.get(key_ssh, 0)
            sftp_count = cache.get(key_sftp, 0)
            if ssh_count > 0:
                hostid = cache.get(key)
                cache.set(key_ssh, ssh_count - 1, timeout=60 * 60 * 24)
                if hostid:
                    # cache.delete(key)
                    self.hostid = hostid
                    if not self.ssh_args:
                        self.set_ssh_args(self.hostid)
                self.tty_args = [term, width, height]
                self.type = 'pty'
            else:
                try:
                    if ssh_count == 0 and sftp_count == 0:
                        cache.delete(key)
                        cache.delete(key_ssh)
                    else:
                        cache.delete(key_ssh)
                except:
                    pass
                finally:
                    self.close()    # 超过随机密码使用次数限制直接断开连接
            return True
        except BaseException:
            self.close()

    def check_channel_subsystem_request(self, channel, name):
        # SFTP子系统
        # print(channel, name, 'subsystem')
        key = 'ssh_%s_%s' % (self.http_user, self.password)
        key_ssh = 'ssh_%s_%s_ssh_count' % (self.http_user, self.password)
        key_sftp = 'ssh_%s_%s_sftp_count' % (self.http_user, self.password)
        try:
            ssh_count = cache.get(key_ssh, 0)
            sftp_count = cache.get(key_sftp, 0)
            if sftp_count > 0:
                hostid = cache.get(key)
                cache.set(key_sftp, sftp_count - 1, timeout=60 * 60 * 24)
                if hostid:
                    # cache.delete(key)
                    self.hostid = hostid
                    if not self.ssh_args:
                        self.set_ssh_args(self.hostid)
                self.type = 'subsystem'
                self.event.set()
            else:
                try:
                    if ssh_count == 0 and sftp_count == 0:
                        cache.delete(key)
                        cache.delete(key_sftp)
                    else:
                        cache.delete(key_sftp)
                except:
                    pass
                finally:
                    self.close()  # 超过随机密码使用次数限制直接断开连接
            return super(ServerInterface, self).check_channel_subsystem_request(channel, name)
        except BaseException:
            self.close()

    def check_channel_window_change_request(self, channel, width, height,
                                            pixelwidth, pixelheight):
        try:
            self.chan_ser.resize_pty(width=width, height=height)    # 必须 try 错误，否则在打开 xshell 后关闭，再连接会出错
        except BaseException:
            pass
        return True

    def check_channel_direct_tcpip_request(self, chan_id, origin, destination):
        # SSH隧道
        self.type = 'direct-tcpip'
        self.event.set()
        return 0
