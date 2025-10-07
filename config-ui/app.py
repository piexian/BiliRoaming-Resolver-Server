import os
import time
import docker
import json
import urllib.parse
try:
    import requests_unixsocket
except Exception:
    requests_unixsocket = None
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- 配置常量 ---
NGINX_CONFIG_DIR = '/config/nginx'
DEFAULT_CONF_PATH = os.path.join(NGINX_CONFIG_DIR, 'default.conf')
FINAL_CONF_PATH = os.path.join(NGINX_CONFIG_DIR, 'final.conf')
CONFIG_LOCK_FILE = os.path.join(NGINX_CONFIG_DIR, '.lock')
NGINX_CONTAINER_NAME = 'biliroaming-proxy'

def make_docker_client():
    """Create a Docker client with fallbacks.

    1. Try docker.from_env() (respects DOCKER_HOST).
    2. If it fails with a URL scheme error like 'http+docker', fall back to unix socket.
    3. If still fails, re-raise the exception.
    """
    # Prefer the unix socket if present — this avoids parsing / handling of
    # DOCKER_HOST values like 'http+docker' that some contexts produce.
    sock_path = '/var/run/docker.sock'
    if os.path.exists(sock_path):
        try:
            app.logger.info('Attempting Docker client via unix socket')
            return docker.DockerClient(base_url=f'unix://{sock_path}')
        except Exception as e:
            app.logger.warning(f'Docker unix socket client failed: {e}')

    # Fall back to environment (this may raise if DOCKER_HOST has an unsupported scheme)
    try:
        app.logger.info('Attempting docker.from_env() fallback')
        return docker.from_env()
    except docker.errors.DockerException as e:
        app.logger.error(f'docker.from_env() failed: {e}')
        # As a last-ditch attempt, explicitly try the unix socket again (different API)
        try:
            return docker.DockerClient(base_url='unix:///var/run/docker.sock')
        except Exception as e2:
            app.logger.error(f'Explicit unix socket fallback also failed: {e2}')
            # Re-raise the original environment error to preserve context
            raise


# Lazy cached docker client. We avoid creating the client at import time to prevent
# the container process from crashing if the host socket or DOCKER_HOST is invalid.
_docker_client = None

def get_docker_client():
    """Return a cached Docker client or attempt to create one.

    Returns None if a client cannot be created. Callers should handle None gracefully.
    """
    global _docker_client
    if _docker_client is not None:
        return _docker_client

    try:
        _docker_client = make_docker_client()
        return _docker_client
    except Exception as e:
        app.logger.error(f"Docker client initialization failed: {e}")
        _docker_client = None
        return None


def docker_unix_session():
    """Return (session, base_url) for requests_unixsocket if available and socket exists."""
    sock = '/var/run/docker.sock'
    if requests_unixsocket is None:
        return None, None
    if not os.path.exists(sock):
        return None, None
    sess = requests_unixsocket.Session()
    # Ensure adapter is mounted for both http+unix and http+docker schemes.
    try:
        from requests_unixsocket import UnixAdapter
        sess.mount('http+unix://', UnixAdapter())
        sess.mount('http+docker://', UnixAdapter())
    except Exception:
        # best-effort; continue
        pass
    base = 'http+unix://' + urllib.parse.quote(sock, safe='')
    return sess, base


def run_certbot_via_http(domain, email, timeout=300):
    """Pull certbot image, create & run certbot container via Docker Engine HTTP API over unix socket.

    Returns (exit_code, logs) or raises Exception on critical failures.
    """
    sess, base = docker_unix_session()
    if sess is None:
        raise RuntimeError('requests_unixsocket not available or /var/run/docker.sock missing')

    image = 'certbot/certbot'
    # 1. Pull image
    pull_url = f"{base}/images/create?fromImage={urllib.parse.quote(image, safe='')}&tag=latest"
    app.logger.info(f'Pulling image {image} via {pull_url}')
    r = sess.post(pull_url, stream=True)
    if r.status_code not in (200, 201):
        raise RuntimeError(f'Failed to pull image {image}: {r.status_code} {r.text}')

    # 2. Create container
    create_url = f"{base}/containers/create"
    name = f"certbot_{int(time.time())}"
    params = {'name': name}
    payload = {
        'Image': image,
        'Cmd': ['certonly', '--standalone', '-d', domain, '--email', email, '--agree-tos', '--no-eff-email'],
        'HostConfig': {
            'Binds': ['letsencrypt_certs:/etc/letsencrypt:rw'],
            'PortBindings': {'80/tcp': [{'HostPort': '80'}]}
        }
    }
    app.logger.info(f'Creating certbot container {name}')
    r = sess.post(create_url + '?' + urllib.parse.urlencode(params), json=payload)
    if r.status_code not in (201, 200):
        raise RuntimeError(f'Failed to create certbot container: {r.status_code} {r.text}')
    container_id = r.json().get('Id')

    try:
        # 3. Start container
        start_url = f"{base}/containers/{container_id}/start"
        r = sess.post(start_url)
        if r.status_code not in (204, 200):
            raise RuntimeError(f'Failed to start certbot container: {r.status_code} {r.text}')

        # 4. Wait for container to finish
        wait_url = f"{base}/containers/{container_id}/wait"
        r = sess.post(wait_url, timeout=timeout)
        if r.status_code not in (200,):
            raise RuntimeError(f'Certbot wait failed: {r.status_code} {r.text}')
        exit_code = r.json().get('StatusCode', 1)

        # 5. Fetch logs
        logs_url = f"{base}/containers/{container_id}/logs?stdout=1&stderr=1"
        r = sess.get(logs_url)
        logs = r.text
        return exit_code, logs
    finally:
        # 6. Remove container
        try:
            rm_url = f"{base}/containers/{container_id}?force=1"
            sess.delete(rm_url)
        except Exception:
            pass


def run_certbot_via_curl(domain, email):
    """Use curl with --unix-socket to pull and run certbot container via Docker Engine API."""
    sock = '/var/run/docker.sock'
    if not os.path.exists(sock):
        raise RuntimeError('socket missing')

    image = 'certbot/certbot:latest'
    # pull
    cmd = f"curl --silent --unix-socket {sock} -X POST \"http://localhost/images/create?fromImage={image.split(':')[0]}&tag=latest\""
    os.system(cmd)

    # create
    import subprocess, json, tempfile
    payload = {
        'Image': image,
        'Cmd': ['certonly', '--standalone', '-d', domain, '--email', email, '--agree-tos', '--no-eff-email'],
        'HostConfig': {
            'Binds': ['letsencrypt_certs:/etc/letsencrypt:rw'],
            'PortBindings': {'80/tcp': [{'HostPort': '80'}]}
        }
    }
    p = subprocess.run(['curl', '--silent', '--unix-socket', sock, '-H', 'Content-Type: application/json', '-X', 'POST', 'http://localhost/containers/create', '--data', json.dumps(payload)], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError('create failed: ' + p.stderr.decode('utf-8'))
    info = json.loads(p.stdout.decode('utf-8'))
    cid = info.get('Id')
    # start
    subprocess.run(['curl', '--silent', '--unix-socket', sock, '-X', 'POST', f'http://localhost/containers/{cid}/start'])
    # wait
    subprocess.run(['curl', '--silent', '--unix-socket', sock, '-X', 'POST', f'http://localhost/containers/{cid}/wait'])
    # logs
    p2 = subprocess.run(['curl', '--silent', '--unix-socket', sock, f'http://localhost/containers/{cid}/logs?stdout=1&stderr=1'], capture_output=True)
    logs = p2.stdout.decode('utf-8', errors='replace')
    # remove
    subprocess.run(['curl', '--silent', '--unix-socket', sock, '-X', 'DELETE', f'http://localhost/containers/{cid}?force=1'])
    return 0, logs

def is_configured():
    return os.path.exists(CONFIG_LOCK_FILE)

@app.before_first_request
def initial_setup():
    """在第一个请求前，确保初始Nginx配置存在"""
    if not is_configured() and not os.path.exists(DEFAULT_CONF_PATH):
        with open('/app/templates/nginx/default.conf.template', 'r') as f_template:
            with open(DEFAULT_CONF_PATH, 'w') as f_config:
                f_config.write(f_template.read())
        # Try to reload nginx in the proxy container if Docker API is available.
        client = get_docker_client()
        if client is None:
            app.logger.warning("Docker client not available; skipping initial nginx reload.")
            return
        try:
            client.containers.get(NGINX_CONTAINER_NAME).exec_run("nginx -s reload")
        except Exception as e:
            app.logger.error(f"Initial Nginx reload failed: {e}")


@app.route('/')
def index():
    if not is_configured():
        return redirect(url_for('setup'))
    
    with open(CONFIG_LOCK_FILE, 'r') as f:
        domain = f.read()
    
    message = f"配置完成！您的 BiliRoaming 解析服务器已在 https://{domain} 上运行。"
    return render_template('setup.html', message=message, success=True, configured=True)

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if is_configured():
        return redirect(url_for('index'))

    if request.method == 'POST':
        domain = request.form['domain'].strip()
        email = request.form['email'].strip()
        session['message'] = f"正在为 {domain} 配置服务，请勿关闭此页面..."
        
        # Acquire Docker client lazily; if not available, inform the user and abort.
        client = get_docker_client()
        if client is None:
            flash("无法访问宿主 Docker API，无法继续自动配置。请检查 /var/run/docker.sock 是否已挂载。", "error")
            return redirect(url_for('setup'))

        try:
            # 1. 停止 Nginx 以释放80端口
            nginx_container = client.containers.get(NGINX_CONTAINER_NAME)
            nginx_container.stop()
            app.logger.info("Nginx container stopped.")
            time.sleep(5) # 等待端口释放

            # 2. 申请SSL证书 - 优先使用 unix-socket curl 方案，回退到 requests_unixsocket 或 docker SDK
            app.logger.info(f"Requesting SSL certificate for {domain}...")
            try:
                exit_code, logs = run_certbot_via_curl(domain, email)
                app.logger.info('certbot via curl logs:\n' + logs[:2000])
            except Exception as e:
                app.logger.warning(f'curl-based certbot failed: {e}. Trying requests_unixsocket/http fallback...')
                try:
                    exit_code, logs = run_certbot_via_http(domain, email)
                    app.logger.info('certbot via http logs:\n' + logs[:2000])
                except Exception as e2:
                    app.logger.error(f'All certbot methods failed: {e2}')
                    raise

            # 3. 生成最终Nginx配置
            with open('/app/templates/nginx/final.conf.template', 'r') as f:
                template = f.read()
            final_config = template.replace('${SERVER_NAME}', domain)
            with open(FINAL_CONF_PATH, 'w') as f:
                f.write(final_config)
            
            # 4. 删除临时配置并创建锁文件
            if os.path.exists(DEFAULT_CONF_PATH):
                os.remove(DEFAULT_CONF_PATH)
            with open(CONFIG_LOCK_FILE, 'w') as f:
                f.write(domain)
            
            # 5. 重新启动Nginx
            nginx_container.start()
            app.logger.info("Nginx container started with final configuration.")
            time.sleep(5) # 等待Nginx启动

            flash("配置成功！", "success")
            return redirect(url_for('index'))

        except docker.errors.ContainerError as e:
            flash(f"证书申请失败: {e.stderr.decode('utf-8')}. 请检查域名解析和端口。", "error")
            try:
                nginx_container.start()
            except Exception:
                app.logger.error("Failed to restart nginx container after certbot error.")
        except Exception as e:
            flash(f"发生未知错误: {e}", "error")
            try:
                nginx_container.start()
            except:
                pass
        
        return redirect(url_for('setup'))

    message = session.pop('message', None)
    return render_template('setup.html', message=message)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
