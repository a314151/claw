import os
web_app_path = "src/personal_assistant/web_app.py"
with open(web_app_path, 'r', encoding='utf-8') as f: web_code = f.read()
if "StreamingResponse" not in web_code:
    web_code = web_code.replace("from fastapi.responses import HTMLResponse", "from fastapi.responses import HTMLResponse, StreamingResponse")
if "/api/chat/stream" not in web_code:
    stream_route = """
@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    from fastapi.responses import StreamingResponse
    import json
    async def event_generator():
        async for chunk in state.assistant.ask_stream(req.message, req.provider, req.model, req.api_key):
            yield f"data: {json.dumps({'content': chunk})}\\n\\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
"""
    with open(web_app_path, 'w', encoding='utf-8') as f: f.write(web_code + stream_route)

# Now edit HTML
html_path = "src/personal_assistant/web/index.html"
with open(html_path, 'r', encoding='utf-8') as f: html_code = f.read()

if "marked.min.js" not in html_code:
    html_code = html_code.replace("</head>", "    <script src=\"https://cdn.jsdelivr.net/npm/marked/marked.min.js\"></script>\n</head>")

if "async function streamMessage" not in html_code:
    js_func = """
    async function streamMessage(message) {
        const payload = {
            message: message,
            provider: null,
            model: null,
            api_key: null
        };
        const response = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let content = '';
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            const chunk = decoder.decode(value, {stream: true});
            const lines = chunk.split('\\\\n');
            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    const data = JSON.parse(line.substring(6));
                    content += data.content;
                    // Update UI here with marked.parse(content)
                }
            }
        }
    }
"""
    html_code = html_code.replace("</script>", js_func + "</script>")
    with open(html_path, 'w', encoding='utf-8') as f: f.write(html_code)

