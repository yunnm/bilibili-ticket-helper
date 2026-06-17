"""GitHub Device Flow 认证 + 推送"""
import requests, json, time, os, sys, base64

REPO_OWNER = "yunnm"
REPO_NAME = "bilibili-ticket-helper"
CLIENT_ID = "178c6fc778ccc68e1d6a"
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def get_token():
    resp = requests.post('https://github.com/login/device/code',
        data={'client_id': CLIENT_ID, 'scope': 'repo'},
        headers={'Accept': 'application/json'}, timeout=15)
    if resp.status_code != 200:
        print('Device flow 启动失败:', resp.text[:200])
        return None
    data = resp.json()
    print(f"\n请在浏览器打开: {data['verification_uri']}")
    print(f"输入验证码: {data['user_code']}")
    print()
    interval = data.get('interval', 5)
    for i in range(60):
        time.sleep(interval)
        resp2 = requests.post('https://github.com/login/oauth/access_token',
            data={'client_id': CLIENT_ID, 'device_code': data['device_code'],
                  'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'},
            headers={'Accept': 'application/json'}, timeout=15)
        result = resp2.json()
        if 'access_token' in result:
            print('认证成功!')
            return result['access_token']
        elif result.get('error') == 'authorization_pending':
            print(f'  等待确认... ({i+1}/60)')
        elif result.get('error') == 'slow_down':
            interval += 5
    return None

def push_files(token):
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
    }
    api = f'https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}'

    # Get the current main branch SHA
    resp = requests.get(f'{api}/git/ref/heads/main', headers=headers, timeout=15)
    if resp.status_code == 404:
        # No commits yet - create initial commit differently
        print("空仓库，正在创建初始提交...")
        return create_initial_commit(token, headers, api)
    
    sha = resp.json()['object']['sha']
    print(f"当前 HEAD: {sha[:7]}")

    # Collect files
    files = collect_files()
    print(f"共 {len(files)} 个文件待推送")

    # Create blobs
    blobs = []
    for path, content in files.items():
        resp = requests.post(f'{api}/git/blobs',
            json={'content': content, 'encoding': 'utf-8'},
            headers=headers, timeout=15)
        blobs.append({
            'path': path,
            'mode': '100644',
            'type': 'blob',
            'sha': resp.json()['sha']
        })
    
    # Create tree
    resp = requests.post(f'{api}/git/trees',
        json={'base_tree': sha, 'tree': blobs},
        headers=headers, timeout=15)
    tree_sha = resp.json()['sha']
    print(f"Tree: {tree_sha[:7]}")

    # Create commit
    resp = requests.post(f'{api}/git/commits',
        json={
            'message': 'feat: Bilibili ticket helper v1.0',
            'tree': tree_sha,
            'parents': [sha],
        },
        headers=headers, timeout=15)
    commit_sha = resp.json()['sha']
    print(f"Commit: {commit_sha[:7]}")

    # Update ref
    resp = requests.patch(f'{api}/git/refs/heads/main',
        json={'sha': commit_sha, 'force': False},
        headers=headers, timeout=15)
    print(f"Push 完成! Status: {resp.status_code}")

def create_initial_commit(token, headers, api):
    files = collect_files()
    blobs = []
    for path, content in files.items():
        resp = requests.post(f'{api}/git/blobs',
            json={'content': content, 'encoding': 'utf-8'},
            headers=headers, timeout=15)
        blobs.append({
            'path': path,
            'mode': '100644',
            'type': 'blob',
            'sha': resp.json()['sha']
        })
    
    resp = requests.post(f'{api}/git/trees',
        json={'tree': blobs}, headers=headers, timeout=15)
    tree_sha = resp.json()['sha']
    
    resp = requests.post(f'{api}/git/commits',
        json={'message': 'feat: Bilibili ticket helper v1.0', 'tree': tree_sha},
        headers=headers, timeout=15)
    commit_sha = resp.json()['sha']
    
    resp = requests.post(f'{api}/git/refs',
        json={'ref': 'refs/heads/main', 'sha': commit_sha},
        headers=headers, timeout=15)
    print(f"初始提交完成! Status: {resp.status_code}")
    return resp.status_code == 201

def collect_files():
    files = {}
    exts = {'.py', '.html', '.md', '.txt', '.yaml', '.gitignore'}
    src_dir = os.path.join(PROJECT_ROOT, 'src')
    
    def add_file(filepath, relpath):
        if relpath.startswith('data') or relpath.startswith('.git') or '__pycache__' in relpath:
            return
        if relpath == 'config.yaml' or relpath.endswith('.bat'):
            return
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                files[relpath.replace('\\', '/')] = f.read()
        except:
            pass

    for root, dirs, filenames in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'data', 'node_modules')]
        for fn in filenames:
            fp = os.path.join(root, fn)
            rp = os.path.relpath(fp, PROJECT_ROOT)
            add_file(fp, rp)
    return files

if __name__ == '__main__':
    token = get_token()
    if token:
        push_files(token)
        print("\n✓ 代码已推送到 GitHub!")
        print(f"  https://github.com/{REPO_OWNER}/{REPO_NAME}")
    else:
        print("\n认证失败，请重试")
