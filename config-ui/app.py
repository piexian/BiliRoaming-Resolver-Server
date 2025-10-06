import os
import time
import docker
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- 配置常量 ---
NGINX_CONFIG_DIR = '/config/nginx'
DEFAULT_CONF_PATH = os.path.join(NGINX_CONFIG_DIR, 'default.conf')
FINAL_CONF_PATH = os.path.join(NGINX_CONFIG_DIR, 'final.conf')
CONFIG_LOCK_FILE = os.path.join(NGINX_CONFIG_DIR, '.lock')
NGINX_CONTAINER_NAME = 'biliroaming-proxy'

client = docker.from_env()

def is_configured():
    return os.path.exists(CONFIG_LOCK_FILE)

@app.before_first_request
def initial_setup():
    """在第一个请求前，确保初始Nginx配置存在"""
    if not is_configured() and not os.path.exists(DEFAULT_CONF_PATH):
        with open('/app/templates/nginx/default.conf.template', 'r') as f_template:
            with open(DEFAULT_CONF_PATH, 'w') as f_config:
                f_config.write(f_template.read())
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
        
        try:
            # 1. 停止 Nginx 以释放80端口
            nginx_container = client.containers.get(NGINX_CONTAINER_NAME)
            nginx_container.stop()
            app.logger.info("Nginx container stopped.")
            time.sleep(5) # 等待端口释放

            # 2. 申请SSL证书
            app.logger.info(f"Requesting SSL certificate for {domain}...")
            client.containers.run(
                "certbot/certbot",
                command=f"certonly --standalone -d {domain} --email {email} --agree-tos --no-eff-email",
                volumes={'letsencrypt_certs': {'bind': '/etc/letsencrypt', 'mode': 'rw'}},
                ports={'80/tcp': 80},
                remove=True
            )
            app.logger.info("SSL certificate obtained successfully.")

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
            nginx_container.start() # 无论如何都要重启Nginx
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
