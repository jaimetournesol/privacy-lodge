#!/usr/bin/env python3
"""Open the private Conductor UI through ephemeral loopback-only Docker tunnels."""
import json
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

port = int(sys.argv[1]) if len(sys.argv)>1 else 18787
if not 1024 <= port <= 65518: raise SystemExit('Choose a port between 1024 and 65518.')
container='privacy-lodge-agent'
try:
    token=subprocess.check_output(['docker','exec','--user','1000:1000',container,'cat','/handoff/agentnode/browser-token'],stderr=subprocess.DEVNULL).decode().strip()
except subprocess.CalledProcessError: raise SystemExit('Conductor is not ready. Check ./pl-box agents status.') from None

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True

class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        source="""import socket,sys,threading
s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),10)
s.settimeout(None)
def up():
 try:
  while b:=sys.stdin.buffer.read1(65536):s.sendall(b)
  s.shutdown(socket.SHUT_WR)
 except OSError:pass
threading.Thread(target=up,daemon=True).start()
try:
 while b:=s.recv(65536):sys.stdout.buffer.write(b);sys.stdout.buffer.flush()
finally:s.close()
"""
        process=subprocess.Popen(['docker','exec','--user','1000:1000','-i',container,'python','-c',source,str(self.server.target)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        def upload():
            try:
                while data:=self.request.recv(65536):process.stdin.write(data);process.stdin.flush()
            except (OSError,ValueError):pass
            finally:
                try:process.stdin.close()
                except OSError:pass
        threading.Thread(target=upload,daemon=True).start()
        try:
            while data:=process.stdout.read1(65536):self.request.sendall(data)
        except OSError:pass
        finally:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait()

servers=[]
try:
    for local,target in [(port,8787)] + [(port+1+slot,4400+slot) for slot in range(17)]:
        server=Server(('127.0.0.1',local),Relay);server.target=target;servers.append(server)
        threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://127.0.0.1:{port}/?lodge=1&tunnel=1#token='+urllib.parse.quote(token,safe='')
    webbrowser.open(url)
    print(f'Conductor opened at http://127.0.0.1:{port}/. Keep this terminal open; Ctrl-C closes the tunnels.')
    threading.Event().wait()
except KeyboardInterrupt:pass
finally:
    for server in servers:server.shutdown();server.server_close()
