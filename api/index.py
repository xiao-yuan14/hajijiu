from http.server import BaseHTTPRequestHandler
import json

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        response = {"status": "ok", "message": "哈基九-玖喵 API 测试成功!"}
        self.wfile.write(json.dumps(response).encode('utf-8'))
        return
    
    def do_POST(self):
        self.do_GET()
