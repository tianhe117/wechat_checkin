"""Server-rendered pages and ordinary POST/redirect/GET forms."""
from datetime import date, datetime, timedelta
import calendar
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from itsdangerous import BadData
from werkzeug.security import check_password_hash, generate_password_hash

from . import PROJECT_ROOT
from .bark import IMAGE_MAX_AGE, screenshot_signer
from .controller_client import ControllerClient
from .planner import MODES, SLOTS, controller_configured, workday
from .storage import Store, TZ
from .tasks import KINDS, Service

STATUS = {"pending": "待执行", "queued": "已受理", "running": "执行中", "completed": "操作完成", "failed": "执行失败", "unknown": "结果未知", "missed": "已错过", "cancelled": "已取消", "skipped": "已跳过", "cleanup": "收尾完成"}
BARK_STATUS = {"pending": "等待通知", "sending": "发送中", "sent": "已发送", "failed": "发送失败", "not_configured": "未配置", "not_needed": "无需通知", "queued": "已受理"}


def create_app(directory=None, *, store=None, service=None, secure_cookie=True):
    app = Flask(__name__, template_folder=str(PROJECT_ROOT/"templates"),
                static_folder=str(PROJECT_ROOT/"static"))
    store = store or Store(directory if directory is not None else PROJECT_ROOT/"data")
    service = service or Service(store)
    app.config.update(SECRET_KEY=store.get("_secret"), PERMANENT_SESSION_LIFETIME=timedelta(days=365),
                      SESSION_REFRESH_EACH_REQUEST=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_NAME="wechat_checkin",
                      SESSION_COOKIE_SECURE=secure_cookie, SESSION_COOKIE_SAMESITE="Lax",
                      MAX_CONTENT_LENGTH=128*1024)
    # Waitress handles headers from the configured trusted Nginx proxy.
    app.extensions.update(store=store, service=service)
    attempts = {}

    def logged_in():
        return session.get("authenticated") is True and session.get("auth_version") == store.get("_auth_version")

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    @app.before_request
    def authorize():
        if request.endpoint in {"static", "manifest", "app_icon", "bark_screenshot"}:
            return
        if request.endpoint != "login" and not logged_in():
            return redirect(url_for("login", next=request.full_path), code=303)
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            if not sent or not secrets.compare_digest(session.get("csrf", ""), sent):
                abort(400, description="页面已过期，请刷新后重新操作")

    @app.after_request
    def headers(response):
        if request.endpoint not in {"static", "manifest", "app_icon"}:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.context_processor
    def context():
        settings = store.settings()
        settings["bark_configured"] = bool(settings.pop("bark_key"))
        settings["controller_configured"] = controller_configured(settings)
        settings["controller_key_configured"] = bool(settings.pop("controller_api_key"))
        return dict(settings=settings, csrf_token=csrf_token, request_id=lambda: secrets.token_urlsafe(18),
                    current_time=service.clock(), statuses=STATUS, bark_statuses=BARK_STATUS, slots=SLOTS, modes=MODES,
                    active="home", device=store.get("_device_state", {}), authenticated=logged_in())

    @app.template_filter("clock")
    def clock_filter(value, fmt="%H:%M:%S"):
        if not value:
            return "—"
        return (datetime.fromisoformat(value) if isinstance(value, str) else value).astimezone(TZ).strftime(fmt)

    @app.template_filter("relative")
    def relative(value):
        day = datetime.fromisoformat(value).astimezone(TZ).date()
        difference = (day-service.clock().date()).days
        return "今天" if difference == 0 else f"{difference} 天后" if difference > 0 else f"{-difference} 天前"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if logged_in():
            return redirect(url_for("overview"))
        error, status = "", 200
        if request.method == "POST":
            key, point = request.remote_addr or "", time.monotonic()
            failures = [value for value in attempts.get(key, []) if point-value < 300]
            if len(failures) >= 5:
                error, status = "尝试次数较多，请 5 分钟后重试", 429
            elif check_password_hash(store.get("_password"), request.form.get("password", "")):
                attempts.pop(key, None)
                session.clear()
                session.update(authenticated=True, auth_version=store.get("_auth_version"), csrf=secrets.token_urlsafe(24))
                session.permanent = True
                target = request.form.get("next", "")
                if not target.startswith("/") or target.startswith("//") or urlsplit(target).netloc:
                    target = url_for("overview")
                return redirect(target, code=303)
            else:
                failures.append(point)
                attempts[key] = failures
                error, status = "密码错误", 400
        return render_template("login.html", error=error, next=request.args.get("next", "")), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"), code=303)

    @app.get("/")
    def overview():
        service.planner.ensure()
        today = service.clock().date()
        plans, overrides = service.planner.plans(today, today)
        with store.connect() as db:
            latest = db.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        return render_template("overview.html", title="概览", subtitle=today.strftime("%m 月 %d 日")+" · 今天的安排",
                               today=today, plans=plans, next_plan=service.planner.next_plan(), mode=overrides.get(today.isoformat(), "default"),
                               latest=store.decode_run(latest) if latest else None, refresh=15)

    @app.post("/pause")
    def pause():
        store.update({"paused": request.form.get("paused") == "1"})
        flash("自动任务已暂停" if store.settings()["paused"] else "自动任务已恢复，错过的计划不会补执行")
        return redirect(url_for("overview"), code=303)

    @app.get("/plans")
    def plans_page():
        today = service.clock().date()
        try:
            first = date.fromisoformat(request.args.get("month", today.strftime("%Y-%m"))+"-01")
            if not 1970 <= first.year <= 2100:
                raise ValueError()
        except ValueError:
            abort(400, description="月份不正确")
        weeks = calendar.Calendar().monthdatescalendar(first.year, first.month)
        plans, overrides = service.planner.plans(weeks[0][0], weeks[-1][-1])
        return render_template("plans.html", title="计划管理", subtitle="按天调整默认、打卡与请假", active="calendar", first=first,
                               weeks=weeks, plans=plans, overrides=overrides, today=today,
                               previous=(first-timedelta(days=1)).strftime("%Y-%m"), following=(first+timedelta(days=32)).strftime("%Y-%m"),
                               is_workday=lambda day: workday(store.settings(), overrides, day))

    @app.route("/plans/edit", methods=["GET", "POST"])
    def edit_plan():
        today = service.clock().date()
        try:
            start = date.fromisoformat(request.values.get("day", today.isoformat()))
            end = date.fromisoformat(request.form.get("end", start.isoformat()))
        except ValueError:
            abort(400, description="日期不正确")
        batch = request.values.get("batch") == "1"
        if request.method == "POST":
            try:
                service.planner.override(start, end if batch else start, request.form.get("mode", "default"))
            except ValueError as error:
                flash(str(error), "error")
            else:
                flash("计划已保存")
                return redirect(url_for("plans_page", month=start.strftime("%Y-%m")), code=303)
        plans, overrides = service.planner.plans(start, start)
        return render_template("edit-plan.html", title="批量调整" if batch else "每日计划", subtitle="修改后更新未来有效计划", active="calendar",
                               back=url_for("plans_page"), start=start, today=today, batch=batch,
                               mode=overrides.get(start.isoformat(), "default"), plans=plans)

    @app.route("/settings/<tab>", methods=["GET", "POST"])
    def settings_page(tab):
        if tab not in {"app", "device", "bark", "account", "schedule"}:
            abort(404)
        if request.method == "POST":
            try:
                values = {}
                if tab == "account":
                    with store.connect(write=True) as db:
                        if not check_password_hash(store.get("_password", db=db), request.form.get("current_password", "")):
                            raise ValueError("当前密码不正确")
                        password = request.form.get("new_password", "")
                        if not password or password != request.form.get("confirm_password"):
                            raise ValueError("请填写新密码，并确保两次输入一致")
                        version = secrets.token_hex(16)
                        store.put(db, "_password", generate_password_hash(password))
                        store.put(db, "_auth_version", version)
                    session["auth_version"] = version
                    session.permanent = True
                elif tab == "app":
                    values = {"package": request.form.get("package", "").strip(), "wait_seconds": int(request.form.get("wait_seconds", "10"))}
                elif tab == "device":
                    values = {"controller_url": request.form.get("controller_url", "").strip().rstrip("/"), "serial": request.form.get("serial", "").strip()}
                    key = request.form.get("controller_api_key", "").strip()
                    if key:
                        values["controller_api_key"] = key
                    if values["controller_url"]:
                        client = ControllerClient(values["controller_url"], key or store.settings()["controller_api_key"])
                        values["controller_url"] = client.controller_url
                elif tab == "schedule":
                    values["weekdays"] = [int(value) for value in request.form.getlist("weekdays")]
                    for slot in SLOTS:
                        values[slot] = {"enabled": request.form.get(slot+"_enabled") == "1", "start": request.form.get(slot+"_start", ""), "end": request.form.get(slot+"_end", "")}
                else:
                    values = {"bark_server": request.form.get("bark_server", "").strip().rstrip("/"), "public_url": request.form.get("public_url", "").strip().rstrip("/")}
                    for value in values.values():
                        parts = urlsplit(value)
                        if value and (parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password):
                            raise ValueError("请填写正确的 HTTP 或 HTTPS 地址")
                    if not values["bark_server"]:
                        raise ValueError("请填写 Bark 地址")
                    key = request.form.get("bark_key", "").strip()
                    if key or request.form.get("clear_bark") == "1":
                        values["bark_key"] = key
                if values:
                    service.planner.update_settings(values)
                    if tab == "device":
                        service.refresh_connection()
                if request.form.get("action") == "test-device" and tab == "device":
                    token = request.form.get("request_id", "")
                    if not token:
                        raise ValueError("页面已过期，请刷新后重新测试")
                    run_id = service.submit("test", token)
                    return redirect(url_for("run_detail", run_id=run_id), code=303)
                if request.form.get("action") == "test-bark" and tab == "bark":
                    identifier = service.create_notice("打卡助手 · 测试通知", "这是一条 Bark 连接测试消息。")
                    return redirect(url_for("notice_page", identifier=identifier), code=303)
            except (ValueError, TypeError) as error:
                flash(str(error) if str(error) else "配置格式不正确", "error")
            else:
                flash("密码已修改，当前设备继续登录" if tab == "account" else "设置已保存")
            return redirect(url_for("settings_page", tab=tab, check="1" if tab == "device" else None), code=303)
        return render_template("settings.html", title="自动计划设置" if tab == "schedule" else "设置", subtitle="应用、设备、通知与账号", tab=tab,
                               active="calendar" if tab == "schedule" else "settings", back=url_for("plans_page") if tab == "schedule" else None,
                               refresh=2 if tab == "device" and request.args.get("check") == "1" and not store.get("_device_state", {}) else None)

    @app.post("/runs")
    def run_create():
        wants_json = request.accept_mimetypes.best == "application/json"

        def navigate(location):
            return {"redirect": location} if wants_json else redirect(location, code=303)

        try:
            token = request.form.get("request_id", "")
            if not token:
                raise ValueError("请求已过期，请刷新页面")
            kind = request.form.get("kind", "full")
            confirmations = None
            if kind == "full":
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", token):
                    raise ValueError("请求已过期，请刷新页面")
                confirmations = session.get("run_confirmations", {})
                question = confirmations.get(token)
                if question and "sequence" not in question:
                    question = None  # Replace pending confirmations from the old arithmetic UI.
                    confirmations.pop(token, None)
                if request.form.get("action") == "cancel":
                    confirmations.pop(token, None)
                    session["run_confirmations"] = confirmations
                    return {"cancelled": True} if wants_json else navigate(question["back"] if question else url_for("overview"))
                # Repeated submissions return the original task after its challenge was consumed.
                with store.connect() as db:
                    previous = db.execute("SELECT id FROM runs WHERE request_id=?", (token,)).fetchone()
                if previous:
                    confirmations.pop(token, None)
                    session["run_confirmations"] = confirmations
                    return navigate(url_for("run_detail", run_id=previous["id"]))
                if not controller_configured(store.settings()):
                    raise ValueError("请先在设备设置中填写控制端地址和密钥")
                if not question or request.form.get("answer", "") != question["sequence"]:
                    if not question:
                        back = request.form.get("return_to", "")
                        if back != "/" and not re.fullmatch(r"/runs/[a-f0-9]{24}", back):
                            back = url_for("overview")
                        rng = secrets.SystemRandom()
                        question = {"sequence": "".join(rng.sample("123456789", 6)),
                                    "grid": rng.sample("123456789", 9), "back": back}
                        # Bound cookie size when users open confirmations in multiple tabs.
                        if len(confirmations) >= 8:
                            confirmations.pop(next(iter(confirmations)))
                        confirmations[token] = question
                    session["run_confirmations"] = confirmations
                    error = "点选顺序不正确，请重新点选。" if "answer" in request.form else ""
                    payload = {"confirmation": {"request_id": token, "sequence": question["sequence"], "grid": question["grid"]}, "error": error}
                    status = 400 if error else 200
                    if wants_json:
                        return payload, status
                    return render_template("confirm-run.html", title="确认打卡", subtitle="请按顺序点击 6 个数字",
                                           confirmation=payload, operation_id=token, return_to=question["back"]), status
            identifier = service.submit(kind, token)
            if confirmations is not None:
                confirmations.pop(token, None)
                session["run_confirmations"] = confirmations
        except ValueError as error:
            flash(str(error), "error")
            return navigate(url_for("settings_page", tab="device" if not controller_configured(store.settings()) else "app"))
        return navigate(url_for("run_detail", run_id=identifier))

    @app.get("/runs")
    def runs_page():
        try:
            page = max(1, int(request.args.get("page", "1")))
            per_page = max(1, min(50, int(request.args.get("per_page", "12"))))
            start = date.fromisoformat(request.args.get("from", (service.clock().date()-timedelta(days=30)).isoformat()))
            end = date.fromisoformat(request.args.get("to", service.clock().date().isoformat()))
        except ValueError:
            abort(400, description="记录查询参数不正确")
        with store.connect() as db:
            count = db.execute("SELECT count(*) FROM runs WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat())).fetchone()[0]
            pages = max(1, (count + per_page - 1) // per_page)
            page = min(page, pages)
            rows = db.execute("SELECT * FROM runs WHERE day BETWEEN ? AND ? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                              (start.isoformat(), end.isoformat(), per_page, (page-1)*per_page)).fetchall()
        return render_template("runs.html", title="执行记录", subtitle="按日期查看操作与截图", active="list", runs=[store.decode_run(r) for r in rows],
                               page=page, pages=pages, per_page=per_page, start=start, end=end)

    @app.get("/runs/<run_id>")
    def run_detail(run_id):
        run = store.run(run_id)
        if not run:
            abort(404)
        return render_template("run.html", title=KINDS.get(run["kind"], "执行详情"), subtitle="记录、步骤与本次截图", active="settings" if run["kind"] == "test" else "list",
                               run=run, back=url_for("runs_page"), refresh=2 if run["status"] in {"queued", "running"} or run["bark_status"] in {"pending", "sending"} else None)

    @app.post("/runs/<run_id>/notify")
    def notify(run_id):
        if not store.run(run_id):
            abort(404)
        service.enqueue_notice(run_id)
        flash("已受理通知重发")
        return redirect(url_for("run_detail", run_id=run_id), code=303)

    @app.get("/screenshots/<run_id>")
    def screenshot(run_id):
        run = store.run(run_id)
        if not run or not run["screenshot"]:
            abort(404, description="本次没有可用截图")
        path = (store.screenshots/run["screenshot"]).resolve()
        if not path.is_relative_to(store.screenshots) or not path.is_file():
            abort(404, description="截图已清理或不可用")
        return send_file(path, mimetype="image/png", conditional=False, download_name=run_id+".png")

    @app.get("/bark/screenshots/<token>.png")
    def bark_screenshot(token):
        try:
            run_id = screenshot_signer(store.get("_secret")).loads(token, max_age=IMAGE_MAX_AGE)
        except BadData:
            abort(404, description="截图链接已失效")
        return screenshot(run_id)

    @app.get("/notices/<identifier>")
    def notice_page(identifier):
        entry = next((item for item in store.get("_notices", []) if item["id"] == identifier), None)
        if not entry:
            abort(404)
        return render_template("notice.html", title="消息通知", subtitle="通知发送结果", active="settings", entry=entry,
                               refresh=2 if entry["status"] == "queued" else None)

    @app.get("/manifest.webmanifest")
    def manifest():
        return {"id": "/", "name": "打卡助手", "short_name": "打卡助手", "lang": "zh-CN", "start_url": "/", "scope": "/", "display": "standalone",
                "background_color": "#f5f6f2", "theme_color": "#245c48", "icons": [{"src": url_for("app_icon", size=size), "sizes": f"{size}x{size}", "type": "image/png", "purpose": "any maskable"} for size in (192, 512)]}

    @app.get("/icons/<int:size>.png")
    def app_icon(size):
        if size not in {192, 512}:
            abort(404)
        return send_file(Path(app.static_folder)/f"icon-{size}.png", mimetype="image/png")

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def error_page(error):
        return render_template("error.html", title="操作提示", subtitle="请检查后重新操作", message=error.description), error.code

    return app
