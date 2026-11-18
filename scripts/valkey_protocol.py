#!/usr/bin/env python3
"""Bounded RESP2 over verified direct TLS; never expose command/error payloads."""

import hashlib
import re
import socket
import ssl
import time


class ReplyError(Exception):
    def __init__(self,value):
        name=value.split(b" ",1)[0].decode("ascii",errors="replace")
        self.code=name if name in {"NOAUTH","WRONGPASS","NOPERM","NOREPLICAS","MASTERDOWN","READONLY","LOADING","NOQUORUM"} else "OTHER"
        super().__init__("Valkey refused a bounded command; private details suppressed")


class Connection:
    def __init__(self,address,hostname,port,context,*,peer_sha256,seconds,deadline,max_bytes,username=None,password=None,bulk_bytes=False):
        if context.verify_mode!=ssl.CERT_REQUIRED or context.check_hostname is not True: raise ValueError("Require verified TLS and endpoint name checking")
        if type(bulk_bytes) is not bool: raise ValueError("Select explicit bounded bulk decoding")
        self.socket=None; self.buffer=bytearray(); self.seconds=seconds; self.deadline=deadline; self.max_bytes=max_bytes; self.bulk_bytes=bulk_bytes
        self.request_deadline=min(deadline,time.monotonic()+seconds)
        try:
            raw=socket.create_connection((address,port),timeout=self.remaining())
            try: self.socket=context.wrap_socket(raw,server_hostname=hostname)
            except BaseException: raw.close(); raise
            if hashlib.sha256(self.socket.getpeercert(binary_form=True)).hexdigest()!=peer_sha256: raise ValueError("The original TLS leaf certificate changed")
            if username is not None and self.command("AUTH",username,password)!="OK": raise ValueError("The scoped identity could not authenticate")
        except BaseException: self.close(); raise
    def remaining(self):
        result=self.request_deadline-time.monotonic()
        if result<=0: raise TimeoutError("The bounded cache request expired")
        return result
    def close(self):
        if self.socket is not None: self.socket.close(); self.socket=None
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
    def receive(self):
        self.socket.settimeout(self.remaining()); chunk=self.socket.recv(min(65536,self.budget+1))
        if not chunk: raise OSError("The bounded TLS response ended early")
        self.budget-=len(chunk)
        if self.budget<0: raise ValueError("Cache response exceeded its byte limit")
        self.buffer.extend(chunk)
    def exact(self,count):
        if count<0 or count>self.max_bytes: raise ValueError("Invalid bounded RESP size")
        while len(self.buffer)<count: self.receive()
        result=bytes(self.buffer[:count]); del self.buffer[:count]; return result
    def line(self):
        while True:
            end=self.buffer.find(b"\r\n")
            if end>=0:
                if end>4096: raise ValueError("RESP line exceeds its bound")
                return self.exact(end+2)[:-2]
            if len(self.buffer)>4098: raise ValueError("RESP line exceeds its bound")
            self.receive()
    def reply(self,depth=0):
        if depth>6: raise ValueError("RESP nesting exceeds its bound")
        line=self.line()
        if not line: raise ValueError("Empty RESP frame")
        kind,value=line[:1],line[1:]
        if kind==b"-": raise ReplyError(value)
        if kind==b"+": return value.decode("utf-8")
        if kind not in {b":",b"$",b"*"} or not re.fullmatch(rb"-?(?:0|[1-9][0-9]{0,12})",value): raise ValueError("Invalid RESP frame")
        count=int(value)
        if kind==b":": return count
        if count==-1: return None
        if count<0: raise ValueError("Invalid RESP count")
        if kind==b"$":
            raw=self.exact(count+2)
            if not raw.endswith(b"\r\n"): raise ValueError("Invalid RESP bulk trailer")
            return raw[:-2] if self.bulk_bytes else raw[:-2].decode("utf-8")
        if count>512: raise ValueError("RESP array exceeds its bound")
        return [self.reply(depth+1) for _ in range(count)]
    def command(self,*arguments):
        if self.buffer: raise ValueError("Unsolicited response bytes cannot become another reply")
        values=[v if isinstance(v,bytes) else str(v).encode("utf-8") for v in arguments]
        if not 1<=len(values)<=32 or any(len(v)>8192 for v in values): raise ValueError("Cache command exceeds its bounds")
        body=b"*"+str(len(values)).encode()+b"\r\n"+b"".join(b"$"+str(len(v)).encode()+b"\r\n"+v+b"\r\n" for v in values)
        if len(body)>16384: raise ValueError("Cache request exceeds its byte limit")
        self.request_deadline=min(self.deadline,time.monotonic()+self.seconds); self.budget=self.max_bytes
        self.socket.settimeout(self.remaining()); self.socket.sendall(body); result=self.reply()
        if self.buffer: raise ValueError("Unexpected additional RESP reply")
        return result
