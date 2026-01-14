import os
import sys
import json
import urllib.parse
import datetime
import time
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# ================= 配置区 =================
ROOT_DIR = r"D:\index\ai_previews"
PORT = 8888
# =========================================

GLOBAL_DB = []
IS_INDEXING = True
SCAN_PROGRESS = {"scanned": 0, "total": 0, "status": "init"}
# 缓存HTML内容，避免每次请求都重新生成
HTML_CACHE = None

# 多线程服务器 (必须保留，防止加载堵塞)
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

def parse_date(folder_name):
    try:
        clean_name = folder_name.replace('.', '-').replace('/', '-')
        parts = clean_name.split('-')
        if len(parts) == 3:
            return datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
    except:
        return None
    return None

def index_worker():
    global GLOBAL_DB, IS_INDEXING, SCAN_PROGRESS
    SCAN_PROGRESS["status"] = "scanning"
    temp_db = []
    try:
        if not os.path.exists(ROOT_DIR): return
        all_items = os.listdir(ROOT_DIR)
        SCAN_PROGRESS["total"] = len(all_items)
        count = 0
        for name in all_items:
            count += 1
            if count % 100 == 0: SCAN_PROGRESS["scanned"] = count
            full_path = os.path.join(ROOT_DIR, name)
            if not os.path.isdir(full_path): continue
            d_obj = parse_date(name)
            if not d_obj: continue
            try:
                # 只读文件名，极速扫描
                with os.scandir(full_path) as it:
                    images = [e.name for e in it if e.is_file() and e.name.lower().endswith(('.jpg','.png','.jpeg','.webp','.bmp'))]
                if images:
                    images.sort(reverse=True)
                    temp_db.append({'date_obj': d_obj, 'folder_name': name, 'images': images})
                    # 增量更新：只在每100个时更新，减少排序次数
                    if len(temp_db) % 100 == 0:
                        GLOBAL_DB = sorted(temp_db, key=lambda x: x['date_obj'], reverse=True)
                        SCAN_PROGRESS["scanned"] = count
            except: continue
            
        # 最终一次性排序，避免重复排序
        GLOBAL_DB = sorted(temp_db, key=lambda x: x['date_obj'], reverse=True)
        IS_INDEXING = False
        SCAN_PROGRESS["status"] = "done"
        SCAN_PROGRESS["scanned"] = SCAN_PROGRESS["total"]
    except Exception as e:
        IS_INDEXING = False

class GalleryHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        decoded_path = urllib.parse.unquote(self.path)
        if self.path == '/':
            global HTML_CACHE
            if HTML_CACHE is None:
                HTML_CACHE = self.get_html().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'public, max-age=3600')  # 缓存HTML 1小时
            self.end_headers()
            self.wfile.write(HTML_CACHE)
            return
        if self.path == '/api/status':
            self.send_response(200)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')  # 状态接口不缓存
            self.end_headers()
            # 优化：使用ensure_ascii=False减少编码开销（如果数据是中文）
            self.wfile.write(json.dumps({"indexing": IS_INDEXING, "progress": SCAN_PROGRESS, "db_size": len(GLOBAL_DB)}, ensure_ascii=False).encode('utf-8'))
            return
        if self.path.startswith('/api/list'):
            self.handle_api_list()
            return
        
        # 处理静态文件请求
        self.handle_static_file()

    def handle_static_file(self):
        """处理静态文件请求，正确解码URL路径"""
        try:
            # 解码URL路径（去掉开头的/）
            path = urllib.parse.unquote(self.path)
            if path.startswith('/'):
                path = path[1:]
            
            # 构建完整文件路径
            file_path = os.path.join(ROOT_DIR, path)
            # 安全检查：确保路径在ROOT_DIR内
            file_path = os.path.normpath(file_path)
            if not file_path.startswith(os.path.normpath(ROOT_DIR)):
                self.send_error(403, "Forbidden")
                return
            
            # 检查文件是否存在
            if not os.path.isfile(file_path):
                self.send_error(404, "File not found")
                return
            
            # 获取文件扩展名确定Content-Type
            ext = os.path.splitext(file_path)[1].lower()
            content_types = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.gif': 'image/gif',
                '.webp': 'image/webp',
                '.bmp': 'image/bmp',
            }
            content_type = content_types.get(ext, 'application/octet-stream')
            
            # 发送文件
            self.send_response(200)
            self.send_header('Content-type', content_type)
            self.end_headers()
            
            with open(file_path, 'rb') as f:
                self.wfile.write(f.read())
        except Exception as e:
            self.send_error(500, str(e))

    def end_headers(self):
        # 强缓存：防止回头看时黑块
        if self.path.lower().endswith(('.jpg', '.png', '.jpeg', '.webp')):
            self.send_header('Cache-Control', 'max-age=31536000, immutable')
        else:
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def handle_api_list(self):
        try:
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            page = int(params.get('page', [0])[0])
            size = int(params.get('size', [10])[0])
            keyword = params.get('q', [''])[0].lower()
            days_filter = params.get('days', [''])[0]
            
            filtered_list = []
            cutoff_date = None
            if days_filter and days_filter.isdigit():
                cutoff_date = datetime.date.today() - datetime.timedelta(days=int(days_filter))

            # 优化：由于GLOBAL_DB已按日期倒序排列，遇到小于cutoff_date的项可以提前退出
            for item in GLOBAL_DB:
                if cutoff_date and item['date_obj'] < cutoff_date:
                    break  # 提前退出，因为后续日期更小
                    
                if keyword:
                    if keyword in item['folder_name'].lower():
                        filtered_list.append(item)
                    else:
                        # 优化：使用生成器表达式，只在找到匹配时才构建列表
                        matched = [i for i in item['images'] if keyword in i.lower()]
                        if matched:
                            filtered_list.append({'date_obj': item['date_obj'], 'folder_name': item['folder_name'], 'images': matched})
                else:
                    filtered_list.append(item)

            start = page * size
            end = start + size
            sliced = filtered_list[start:end]
            result = [{"date": i['folder_name'], "images": i['images']} for i in sliced]
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')  # API数据不缓存
            self.end_headers()
            # 优化：使用ensure_ascii=False减少编码开销
            self.wfile.write(json.dumps({"data": result, "has_more": end < len(filtered_list)}, ensure_ascii=False).encode('utf-8'))
        except: self.send_error(500)

    def get_html(self):
        return """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>AI Gallery V15 (Final)</title>
<style>
    body { background: #121212; color: #ddd; font-family: sans-serif; margin: 0; padding-top: 100px; }
    
    .header { 
        position: fixed; top: 0; left: 0; right: 0; height: auto; min-height: 60px; background: #1e1e1e; 
        display: flex; flex-wrap: wrap; align-items: center; padding: 10px 20px; z-index: 999; border-bottom: 1px solid #333;
        gap: 10px;
    }
    .search { background: #333; border: 1px solid #555; color: #fff; padding: 8px 15px; border-radius: 4px; width: 250px; }
    
    /* 按钮组样式优化 */
    .btns { display: flex; gap: 6px; flex-wrap: wrap; }
    .btn { 
        background: #2b2b2b; color: #aaa; border: 1px solid #444; 
        padding: 5px 12px; cursor: pointer; border-radius: 15px; font-size: 13px; transition: 0.2s;
    }
    .btn:hover { background: #444; color: #fff; }
    .btn.active { background: #00bcd4; color: #000; font-weight: bold; border-color: #00bcd4; }

    .status { margin-left: auto; font-size: 12px; color: #666; white-space: nowrap; }
    .progress { position: absolute; bottom: 0; left: 0; height: 3px; background: #00bcd4; width: 0%; transition: 0.5s; }

    .grid {
        display: grid;
        /* 强制格子布局，解决黑块塌陷 */
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
        gap: 8px;
        /* 优化渲染性能 */
        contain: layout style paint;
    }

    /* === 核心优化：渲染性能 === */
    .section { 
        margin: 20px; 
        /* 【关键】这行代码让屏幕外的内容不计算布局，解决卡顿 */
        content-visibility: auto; 
        contain-intrinsic-size: 500px; /* 给一个预估高度，防止滚动条抖动 */
        /* 开启GPU加速，提高滚动性能 */
        will-change: scroll-position;
        transform: translateZ(0);
    }
    
    .title { color: #00bcd4; font-size: 1.2rem; margin-bottom: 10px; border-bottom: 1px solid #333; padding-bottom: 5px; }

    .card {
        aspect-ratio: 1; 
        background: #202020; 
        border-radius: 6px; overflow: hidden; position: relative; border: 1px solid #333;
        /* 开启GPU加速，提高渲染性能 */
        transform: translateZ(0);
        backface-visibility: hidden;
        -webkit-backface-visibility: hidden;
    }

    .card img {
        width: 100%; height: 100%; object-fit: cover; display: block;
        opacity: 0; transition: opacity 0.3s;
    }
    .card img.loaded { opacity: 1; }
    .card img.error { opacity: 0.5; filter: grayscale(100%); }

    .card .name {
        position: absolute; bottom: 0; width: 100%; background: rgba(0,0,0,0.7);
        font-size: 10px; text-align: center; color: #fff; padding: 2px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    
    #loading { text-align: center; padding: 50px; color: #666; }
</style>
</head>
<body>

<div class="header">
    <input type="text" class="search" id="q" placeholder="🔍 搜索...">
    <div class="btns">
        <button class="btn active" onclick="filter('',this)">全部</button>
        <button class="btn" onclick="filter('3',this)">3天</button>
        <button class="btn" onclick="filter('7',this)">7天</button>
        <button class="btn" onclick="filter('15',this)">半月</button>
        <button class="btn" onclick="filter('30',this)">1月</button>
        <button class="btn" onclick="filter('90',this)">3月</button>
        <button class="btn" onclick="filter('180',this)">6月</button>
        <button class="btn" onclick="filter('365',this)">全年</button>
    </div>
    <div class="status" id="st">初始化...</div>
    <div class="progress" id="pg"></div>
</div>

<div id="app"></div>
<div id="loading">...</div>

<script>
    let page=0, isLoading=false, hasMore=true, q="", days="", indexing=true;
    
    // DOM清理机制：限制内存占用（针对20万+图片优化）
    const MAX_SECTIONS = 200; // 最多保留200个section
    let cleanupTimer = null;
    let lastCleanupScrollY = 0;
    
    function cleanupDistantSections() {
        // 避免滚动时频繁清理，只在停止滚动后清理
        const sections = document.querySelectorAll('.section');
        if (sections.length <= MAX_SECTIONS) return;
        
        // 简单策略：移除最前面的section（已经在视口上方很远的）
        const sectionsArray = Array.from(sections);
        const viewportTop = window.scrollY;
        const viewportHeight = window.innerHeight;
        
        // 只移除视口上方超过5屏的section，避免影响滚动
        let removed = 0;
        for (let i = 0; i < sectionsArray.length - MAX_SECTIONS && removed < 20; i++) {
            const section = sectionsArray[i];
            // 使用简单的偏移量估计，避免getBoundingClientRect强制重排
            const sectionIndex = i;
            const estimatedTop = sectionIndex * 600; // 估算每个section约600px高
            
            if (estimatedTop < viewportTop - viewportHeight * 5) {
                section.remove();
                removed++;
            }
        }
    }
    
    // 自动重试机制：解决个别图片加载失败
    window.handleError = function(img) {
        if (!img.dataset.retried) {
            img.dataset.retried = "true";
            console.warn('图片加载失败，1秒后重试:', img.src);
            setTimeout(() => { img.src = img.src; }, 1000); // 1秒后重试
        } else {
            console.error('图片加载失败（已重试）:', img.src);
            img.classList.add('error');
        }
    };

    async function check() {
        if(!indexing) return;
        let res = await fetch('/api/status');
        let d = await res.json();
        indexing = d.indexing;
        if(d.progress.total>0) document.getElementById('pg').style.width = (d.progress.scanned/d.progress.total*100)+"%";
        document.getElementById('st').innerText = indexing ? `扫描中 ${d.progress.scanned}` : `共 ${d.db_size} 天`;
        if(indexing) setTimeout(check, 1000);
    }

    function filter(d, btn) {
        document.querySelectorAll('.btn').forEach(b=>b.classList.remove('active'));
        btn.classList.add('active');
        days=d; q=""; document.getElementById('q').value="";
        reset();
    }

    function reset() {
        page=0; hasMore=true; 
        if (cleanupTimer) clearTimeout(cleanupTimer);
        document.getElementById('app').innerHTML="";
        window.scrollTo(0,0); load();
    }

    async function load() {
        if(isLoading || !hasMore) return;
        isLoading=true; document.getElementById('loading').style.display='block';
        
        try {
            let res = await fetch(`/api/list?page=${page}&size=10&q=${q}&days=${days}`);
            let json = await res.json();
            
            if(json.data.length==0 && page==0) document.getElementById('app').innerHTML = '<div style="padding:40px;text-align:center">暂无数据</div>';
            
            // 使用 DocumentFragment 批量插入，减少重绘
            let fragment = document.createDocumentFragment();
            
            json.data.forEach(item => {
                let div = document.createElement('div');
                div.className = 'section';
                
                // 优化：使用数组join代替字符串拼接，性能更好
                let gridHtml = item.images.map(img => {
                    // 修复：分别编码路径各部分，保持斜杠不变（路径编码）
                    let src = encodeURIComponent(item.date) + '/' + encodeURIComponent(img);
                    let imgName = img.replace(/</g, '&lt;').replace(/>/g, '&gt;'); // XSS防护
                    return `<div class="card">
                        <img src="${src}" loading="lazy" onload="this.classList.add('loaded')" onerror="handleError(this)">
                        <div class="name">${imgName}</div>
                    </div>`;
                }).join('');
                
                div.innerHTML = `<div class="title">${item.date} <small>(${item.images.length})</small></div><div class="grid">${gridHtml}</div>`;
                fragment.appendChild(div);
            });
            
            document.getElementById('app').appendChild(fragment);
            
            // 延迟清理，避免频繁操作DOM（只在加载新内容后清理）
            if (cleanupTimer) clearTimeout(cleanupTimer);
            cleanupTimer = setTimeout(() => {
                const sectionCount = document.querySelectorAll('.section').length;
                if (sectionCount > MAX_SECTIONS * 1.5) {
                    cleanupDistantSections();
                }
            }, 2000);
            
            hasMore = json.has_more;
            page++;
        } catch(e) {console.error(e);}
        finally { isLoading=false; if(!hasMore) document.getElementById('loading').style.display='none'; }
    }

    let t;
    document.getElementById('q').addEventListener('input', e=>{
        clearTimeout(t); t=setTimeout(()=>{ q=e.target.value; reset(); }, 300);
    });

    // 优化：滚动事件节流，提高滚动性能
    let scrollTimer = null;
    let lastScrollY = 0;
    let scrollDirection = 0;
    
    function handleScroll() {
        const currentScrollY = window.scrollY;
        scrollDirection = currentScrollY > lastScrollY ? 1 : -1;
        lastScrollY = currentScrollY;
        
        // 检查是否需要加载更多
        if ((window.innerHeight + currentScrollY) >= document.body.offsetHeight - 1500) {
            load();
        }
    }
    
    // 使用被动事件监听器，提高滚动性能
    window.addEventListener('scroll', () => {
        if (scrollTimer) return;
        scrollTimer = requestAnimationFrame(() => {
            handleScroll();
            scrollTimer = null;
        });
    }, { passive: true });

    check();
    load();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    t = threading.Thread(target=index_worker)
    t.daemon = True
    t.start()
    print(f"V15 Final: http://localhost:{PORT}")
    import webbrowser
    webbrowser.open(f'http://localhost:{PORT}')
    server = ThreadingHTTPServer(('localhost', PORT), GalleryHandler)
    server.serve_forever()

# print("demo for Graphite PR")