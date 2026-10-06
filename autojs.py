#coding:utf-8
#
# AutoJs Sublime Text 插件
#
# 支持两种手机端:
#   1. 新版 AutoX.js: WebSocket 协议(端口 9317),手机端 "连接电脑" 中填写 电脑IP:9317
#   2. 旧版 Auto.js: 裸 JSON 协议(端口 1209,每行一个 JSON 对象)
#
import base64
import hashlib
import json
import random
import socket
import struct
import threading
import time
import traceback

import sublime
import sublime_plugin

WS_PORT = 9317
LEGACY_PORT = 1209

HANDSHAKE_TIMEOUT = 10  # 握手及等待 hello 的超时(秒)
SERVER_VERSION = '1.110.0'  # 声明给手机端的版本,>=1.110.0 时新版 app 要求 hello 应答 data == "ok"
MIN_APP_VERSION_CODE = 629  # app_version_code >= 629 回复 "ok",否则回复 "连接成功"
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BYTES = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def _get_host_ip():
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        if s is not None:
            s.close()


def _message_id():
    return '%d_%s' % (int(time.time() * 1000), random.random())


def _xor_mask(data, mask):
    out = bytearray(len(data))
    for i in range(len(data)):
        out[i] = data[i] ^ mask[i % 4]
    return bytes(out)


def _status_message(message):
    try:
        sublime.set_timeout(lambda: sublime.status_message(message), 0)
    except Exception:
        pass


def _script_id(name, view_id):
    # 手机端用 id 区分脚本执行实例,stop 时需要和 run 的 id 匹配
    if name:
        return name
    return 'sublime-view-%s' % view_id


def _ws_handshake(sock):
    # 读取 HTTP 升级请求并完成 WebSocket 握手,返回握手后剩余的字节;失败返回 None
    data = b''
    while b'\r\n\r\n' not in data:
        try:
            chunk = sock.recv(4096)
        except Exception:
            return None
        if not chunk:
            return None
        data += chunk
        if len(data) > 65536:
            return None
    head, _, leftover = data.partition(b'\r\n\r\n')
    key = None
    for line in head.decode('utf-8', 'replace').split('\r\n')[1:]:
        if ':' not in line:
            continue
        field, value = line.split(':', 1)
        if field.strip().lower() == 'sec-websocket-key':
            key = value.strip()
            break
    if not key:
        try:
            sock.sendall(b'HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n')
        except Exception:
            pass
        return None
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode('utf-8')).digest()).decode('ascii')
    response = ('HTTP/1.1 101 Switching Protocols\r\n'
                'Upgrade: websocket\r\n'
                'Connection: Upgrade\r\n'
                'Sec-WebSocket-Accept: ' + accept + '\r\n'
                '\r\n')
    try:
        sock.sendall(response.encode('ascii'))
    except Exception:
        return None
    return leftover


class WsConnection(object):
    """一个 WebSocket 连接(新版 AutoX.js)"""

    def __init__(self, sock, addr, leftover=b''):
        self.sock = sock
        self.addr = addr
        self.attached = False  # 已完成 hello 交互
        self.device_name = ''
        self._buffer = leftover
        self._write_lock = threading.Lock()

    def _recv(self, n):
        if self._buffer:
            chunk = self._buffer[:n]
            self._buffer = self._buffer[n:]
            return chunk
        return self.sock.recv(n)

    def _recv_exact(self, n):
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self._recv(min(remaining, 65536))
            if not chunk:
                raise ConnectionError('连接已关闭')
            chunks.append(chunk)
            remaining -= len(chunk)
        if len(chunks) == 1:
            return chunks[0]
        return b''.join(chunks)

    def _read_frame(self):
        header = self._recv_exact(2)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0f
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7f
        if length == 126:
            length = struct.unpack('!H', self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b''
        if mask is not None:
            payload = _xor_mask(payload, mask)
        return fin, opcode, payload

    def read_message(self):
        fragments = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                # 手机端每 10 秒发送一次控制帧 ping,必须回 pong
                self.send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self.send_frame(OP_CLOSE, payload)
                except Exception:
                    pass
                raise ConnectionError('对端主动关闭连接')
            if opcode in (OP_TEXT, OP_BYTES, OP_CONT):
                fragments.append(payload)
                if fin:
                    return b''.join(fragments)
                continue
            raise ConnectionError('未知的 WebSocket opcode: 0x%x' % opcode)

    def send_frame(self, opcode, payload=b''):
        # 服务端 -> 客户端的数据帧不掩码
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack('!H', length))
        else:
            header.append(127)
            header.extend(struct.pack('!Q', length))
        with self._write_lock:
            self.sock.sendall(bytes(header) + payload)

    def send_json(self, obj):
        text = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
        self.send_frame(OP_TEXT, text.encode('utf-8'))

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def serve(self):
        # 1) 等待 hello(超时 10 秒)
        self.sock.settimeout(HANDSHAKE_TIMEOUT)
        try:
            first = self.read_message()
        except socket.timeout:
            print('AutoJs: 等待 hello 超时')
            return
        except Exception as e:
            print('AutoJs: 连接中断: %s' % e)
            return
        try:
            obj = json.loads(first.decode('utf-8', 'replace'))
        except Exception:
            print('AutoJs: 首条消息不是合法 JSON: %s' % first[:200])
            return
        if not isinstance(obj, dict) or obj.get('type') != 'hello':
            print('AutoJs: 首条消息不是 hello: %s' % first[:200])
            return
        self._handle_hello(obj)
        self.sock.settimeout(None)
        # 2) 处理后续消息(log / ping / close)
        while True:
            try:
                message = self.read_message()
            except Exception as e:
                print('AutoJs: 连接结束(%s)' % e)
                return
            try:
                self._handle_message(message)
            except ConnectionError as e:
                print('AutoJs: %s' % e)
                return
            except Exception as e:
                print('AutoJs: 处理消息出错: %s' % e)

    def _handle_hello(self, obj):
        data = obj.get('data')
        app_version_code = 0
        if isinstance(data, dict):
            try:
                app_version_code = int(data.get('app_version_code') or 0)
            except (TypeError, ValueError):
                app_version_code = 0
            self.device_name = data.get('device_name') or ''
        if app_version_code >= MIN_APP_VERSION_CODE:
            reply_data = 'ok'
        else:
            reply_data = u'连接成功'
        self.send_json({
            'message_id': obj.get('message_id'),
            'data': reply_data,
            'version': SERVER_VERSION,
            'debug': False,
            'type': 'hello',
        })
        self.attached = True
        name = self.device_name or ('%s:%s' % self.addr)
        print('AutoJs: 设备已连接: %s (app_version_code=%s)' % (name, app_version_code))
        _status_message('AutoJs 已连接: %s' % name)

    def _handle_message(self, message):
        try:
            obj = json.loads(message.decode('utf-8', 'replace'))
        except Exception:
            print('AutoJs: 无法解析消息: %s' % message[:200])
            return
        if not isinstance(obj, dict):
            return
        msg_type = obj.get('type')
        if msg_type == 'ping':
            # {"type":"ping","data":...} 必须回复相同 data 的 pong,否则手机端断线重连
            self.send_json({'type': 'pong', 'data': obj.get('data')})
        elif msg_type == 'pong':
            pass
        elif msg_type == 'log':
            log = obj.get('log')
            if log is None:
                d = obj.get('data')
                if isinstance(d, dict):
                    log = d.get('log')
                elif isinstance(d, str):
                    log = d
            print('Log: %s' % log)
        elif msg_type == 'toast':
            print('Toast: %s' % obj.get('data'))
        elif msg_type == 'close':
            raise ConnectionError('设备主动断开连接')
        elif msg_type == 'hello':
            self._handle_hello(obj)
        else:
            print('AutoJs: 未知消息: %s' % message[:300])


class WebSocketServer(object):
    """WebSocket 服务端(新版 AutoX.js,默认端口 9317)"""

    def __init__(self, port):
        self.port = port
        self._sock = None
        self._running = False
        self._conns = []
        self._lock = threading.Lock()

    def is_running(self):
        return self._running

    def has_conn(self):
        with self._lock:
            return any(c.attached for c in self._conns)

    def start(self, host=''):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, self.port))
            sock.listen(8)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        self._sock = sock
        self._running = True
        t = threading.Thread(target=self._accept_loop)
        t.daemon = True
        t.start()

    def stop(self):
        self._running = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            conn.close()

    def _accept_loop(self):
        sock = self._sock
        while self._running:
            try:
                conn_sock, addr = sock.accept()
            except Exception as e:
                if self._running:
                    print('AutoJs: WebSocket accept 出错: %s' % e)
                break
            t = threading.Thread(target=self._serve_conn, args=(conn_sock, addr))
            t.daemon = True
            t.start()

    def _serve_conn(self, sock, addr):
        conn = None
        try:
            sock.settimeout(HANDSHAKE_TIMEOUT)
            leftover = _ws_handshake(sock)
            if leftover is None:
                try:
                    sock.close()
                except Exception:
                    pass
                return
            conn = WsConnection(sock, addr, leftover)
            with self._lock:
                self._conns.append(conn)
            print('AutoJs: WebSocket 连接来自 %s:%s,等待 hello...' % (addr[0], addr[1]))
            conn.serve()
        except Exception as e:
            print('AutoJs: WebSocket 连接异常: %s' % e)
        finally:
            if conn is not None:
                with self._lock:
                    if conn in self._conns:
                        self._conns.remove(conn)
                conn.close()
                print('AutoJs: 设备已断开: %s:%s' % (addr[0], addr[1]))

    def send_command(self, command, script_id=None, name=None, script=None):
        with self._lock:
            conns = [c for c in self._conns if c.attached]
        if not conns:
            return False
        data = {'command': command}
        if command == 'stop':
            data['id'] = script_id
        elif command != 'stopAll':
            data['id'] = script_id
            data['name'] = name
            data['script'] = script
        message = {'type': 'command', 'message_id': _message_id(), 'data': data}
        sent = False
        for conn in conns:
            try:
                conn.send_json(message)
                sent = True
            except Exception as e:
                print('AutoJs: WebSocket 发送失败: %s' % e)
        return sent


class LegacyServer(object):
    """旧版 Auto.js(默认端口 1209),裸 JSON 协议,每行一个 JSON 对象"""

    def __init__(self, port):
        self.port = port
        self._sock = None
        self._running = False
        self._conns = []
        self._lock = threading.Lock()

    def is_running(self):
        return self._running

    def has_conn(self):
        with self._lock:
            return len(self._conns) > 0

    def start(self, host=''):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, self.port))
            sock.listen(8)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            raise
        self._sock = sock
        self._running = True
        t = threading.Thread(target=self._accept_loop)
        t.daemon = True
        t.start()

    def stop(self):
        self._running = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

    def _accept_loop(self):
        sock = self._sock
        while self._running:
            try:
                conn, addr = sock.accept()
            except Exception as e:
                if self._running:
                    print('AutoJs: 旧协议 accept 出错: %s' % e)
                break
            print('已连接: {0}:{1}'.format(addr[0], addr[1]))
            _status_message('{0}:{1} connected'.format(addr[0], addr[1]))
            with self._lock:
                self._conns.append(conn)
            t = threading.Thread(target=self._serve_conn, args=(conn, addr))
            t.daemon = True
            t.start()

    def _serve_conn(self, conn, addr):
        try:
            f = conn.makefile('rb')
            for raw in f:
                line = raw.decode('utf-8', 'replace').strip()
                if not line:
                    continue
                try:
                    # 修复 Issue #249: 一次读到多条 JSON 对象 {"type":"log",...}{"type":"log",...}
                    if line.find('}{') != -1:
                        for item in line.split('}{'):
                            if item.find('{') == -1:
                                item = '{' + item
                            if item.find('}') == -1:
                                item = item + '}'
                            self._on_receive(item)
                        continue
                    self._on_receive(line)
                except Exception as ex:
                    print('Error line: %s' % line)
                    print('%s: %s' % (Exception, ex))
                    traceback.print_exc()
        except Exception as e:
            print('%s: %s' % (Exception, e))
        finally:
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except Exception:
                pass

    def _on_receive(self, line):
        data = json.loads(line)
        if not isinstance(data, dict):
            return
        if data.get('type') == 'log':
            log = data.get('log')
            if log is None:
                d = data.get('data')
                if isinstance(d, dict):
                    log = d.get('log')
            print('Log: %s' % log)

    def send_command(self, command, view_id=None, name=None, script=None):
        with self._lock:
            conns = list(self._conns)
        if not conns:
            return False
        if command == 'stopAll':
            obj = {'type': 'command', 'command': 'stopAll'}
        elif command == 'stop':
            obj = {'type': 'command', 'view_id': view_id, 'command': 'stop'}
        else:
            obj = {'type': 'command', 'view_id': view_id, 'name': name, 'command': command, 'script': script}
        payload = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
        sent = False
        for conn in conns:
            try:
                conn.sendall(payload)
                sent = True
            except Exception as e:
                print('AutoJs: 旧协议发送失败: %s' % e)
        return sent


class AutoJsServer(object):
    """同时开启 WebSocket(9317,新版 AutoX.js)和旧协议(1209,旧版 Auto.js)两个服务"""

    def __init__(self):
        self.ws_server = WebSocketServer(WS_PORT)
        self.legacy_server = LegacyServer(LEGACY_PORT)
        self.ip = None

    def get_host_ip(self):
        if self.ip is None:
            self.ip = _get_host_ip()
        return self.ip

    def is_running(self):
        return self.ws_server.is_running() or self.legacy_server.is_running()

    def any_connected(self):
        return self.ws_server.has_conn() or self.legacy_server.has_conn()

    def connect(self):
        if self.is_running():
            print('服务正在运行中(请连接:%s)...' % self.get_host_ip())
            return
        self.ip = self.get_host_ip()
        started = []
        try:
            self.ws_server.start()
            started.append((u'新版 AutoX.js (WebSocket)', WS_PORT))
        except Exception as e:
            print('AutoJs: WebSocket 服务(端口 %d)启动失败: %s' % (WS_PORT, e))
            traceback.print_exc()
        try:
            self.legacy_server.start()
            started.append((u'旧版 Auto.js', LEGACY_PORT))
        except Exception as e:
            print('AutoJs: 旧协议服务(端口 %d)启动失败: %s' % (LEGACY_PORT, e))
            traceback.print_exc()
        if not started:
            sublime.error_message(u'无法启动 AutoJs 服务,请查看 Sublime 控制台。')
            return
        print('等待连接...')
        for label, port in started:
            print(u'[%s] 请连接至: %s:%d' % (label, self.ip, port))

    def disconnect(self):
        running = self.is_running()
        self.ws_server.stop()
        self.legacy_server.stop()
        self.ip = None
        if running:
            print('断开连接')

    def send(self, command, view_id=None, name=None, script=None):
        ws_sent = self.ws_server.send_command(command, _script_id(name, view_id), name, script)
        legacy_sent = self.legacy_server.send_command(command, view_id, name, script)
        if not ws_sent and not legacy_sent:
            sublime.error_message(u'请先连接到设备！')
        else:
            print('send: %s' % command)


server = AutoJsServer()


class RunCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        content = self.view.substr(sublime.Region(0, self.view.size()))
        server.send('run', view_id=self.view.id(), name=self.view.file_name(), script=content)


class StopCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        server.send('stop', view_id=self.view.id(), name=self.view.file_name())


class RerunCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        content = self.view.substr(sublime.Region(0, self.view.size()))
        server.send('rerun', view_id=self.view.id(), name=self.view.file_name(), script=content)


class StopAllCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        server.send('stopAll')


class SaveToPhoneCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        content = self.view.substr(sublime.Region(0, self.view.size()))
        server.send('save', view_id=self.view.id(), name=self.view.file_name(), script=content)


class ConnectCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        server.connect()


class DisconnectCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        server.disconnect()


def plugin_unloaded():
    server.disconnect()
