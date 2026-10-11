from __future__ import annotations
import json, os, secrets, sqlite3
from datetime import timedelta
from functools import wraps
from pathlib import Path
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from ability_allocator import X, M, N_STEPS, INITIAL_XP, PlayerInput, PlayerState, solve_step, validate_final

BASE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("DATABASE_PATH", str(BASE / "game.db"))
ROOM_CODE = os.environ.get("ROOM_CODE", "CHANGE-ME")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-before-sharing")
app.permanent_session_lifetime = timedelta(days=14)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1")

def connect():
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c

def initial_game():
    return {"schema_version": 3, "player_count": X, "single_player_test": X == 1,
        "phase":"lobby","round":1,"players":[
        {"id":i,"name":None,"token":None,"ready":False,"submitted":False,"input":None,"order":None,
         "state":{"values":[0]*M,"used":[False]*M,"xp":INITIAL_XP,"total_bid_paid":0,"total_bid_refunded":0}}
        for i in range(X)], "next_order":1,"results":{},"error":None}

def init_db():
    with connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS game_state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        row = c.execute("SELECT data FROM game_state WHERE id=1").fetchone()
        if row is None:
            c.execute("INSERT INTO game_state(id,data) VALUES(1,?)",(json.dumps(initial_game(),ensure_ascii=False),))
        else:
            try:
                old = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError):
                old = {}
            # 参加人数やデータ形式が変わった場合、古い状態を誤用しないよう初期化する。
            if old.get("schema_version") != 3 or old.get("player_count") != X or len(old.get("players", [])) != X:
                c.execute("UPDATE game_state SET data=? WHERE id=1",(json.dumps(initial_game(),ensure_ascii=False),))

def load(c):
    row=c.execute("SELECT data FROM game_state WHERE id=1").fetchone()
    if row is None: raise RuntimeError("ゲーム状態がありません")
    return json.loads(row["data"])

def save(c,g):
    c.execute("UPDATE game_state SET data=? WHERE id=1",(json.dumps(g,ensure_ascii=False),))

def get_player(g,pid):
    return g["players"][pid] if isinstance(pid,int) and 0 <= pid < X else None

def owns(p):
    a=session.get("token",""); b=(p or {}).get("token") or ""
    return bool(a and b and secrets.compare_digest(a,b))

def current():
    with connect() as c: g=load(c)
    p=get_player(g,session.get("player_id"))
    if not owns(p):
        session.clear()
        return None,None
    return g,p

def login_required(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        if "player_id" not in session: return redirect(url_for("login"))
        return fn(*a,**kw)
    return wrapped

'''
@app.context_processor
def constants():
    return {"player_count":X,"ability_count":M,"round_count":N_STEPS,"single_player_test":X == 1}
'''
# 能力値の操作を書き換える。
@app.context_processor
def constants():
    # M の数（能力値の数）に合わせて、名前をリストで定義します
    names = ["運動", "銃器", "接近戦", "投擲", "科学", "心理学", "官僚主義", "アルファコンプレックス","ごまかし", "魅惑", "威圧", "秘密行動", "操作", "機械工作", "プログラム", "爆破"] # ※数はMに合わせて増減させてください
    return {
        "player_count": X,
        "ability_count": M,
        "round_count": N_STEPS,
        "ability_names": names  # これを追加！
    }

@app.route("/login",methods=["GET","POST"])
def login():
    if "player_id" in session: return redirect(url_for("home"))
    with connect() as c: g=load(c)
    available=[{"id":p["id"],"name":p["name"],"taken":bool(p["name"])} for p in g["players"]]
    if request.method=="POST":
        code=request.form.get("room_code","").strip()
        name=request.form.get("name","").strip()
        try: pid=int(request.form.get("player_id","-1"))
        except ValueError: pid=-1
        if not secrets.compare_digest(code,ROOM_CODE):
            flash("ルームコードが違います。","error")
        elif not name or len(name)>24:
            flash("表示名は1〜24文字で入力してください。","error")
        elif not 0 <= pid < X:
            flash("プレイヤー番号を選んでください。","error")
        else:
            c=connect()
            try:
                c.execute("BEGIN IMMEDIATE"); g=load(c)
                if g["phase"]!="lobby":
                    c.rollback(); flash("ゲーム開始後は新規参加できません。","error")
                elif g["players"][pid]["name"] is not None:
                    c.rollback(); flash("そのプレイヤー枠は使用中です。","error")
                else:
                    token=secrets.token_urlsafe(32)
                    g["players"][pid]["name"]=name; g["players"][pid]["token"]=token
                    save(c,g); c.commit()
                    session.clear(); session.permanent=True
                    session["player_id"]=pid; session["token"]=token
                    return redirect(url_for("home"))
            except Exception:
                c.rollback(); raise
            finally: c.close()
        with connect() as c: g=load(c)
        available=[{"id":p["id"],"name":p["name"],"taken":bool(p["name"])} for p in g["players"]]
    return render_template("login.html",available=available)

@app.route("/")
@login_required
def home():
    g,p=current()
    if g is None: return redirect(url_for("login"))
    if g["phase"]=="results": return redirect(url_for("results"))
    if g["phase"]=="finished": return redirect(url_for("final_results"))
    if g["phase"]=="input" and not p["submitted"]: return redirect(url_for("round_page"))
    return render_template("lobby.html",game=g,player=p)

@app.post("/ready")
@login_required
def ready():
    c=connect()
    try:
        c.execute("BEGIN IMMEDIATE"); g=load(c); p=get_player(g,session.get("player_id"))
        if not owns(p): c.rollback(); session.clear(); return redirect(url_for("login"))
        if g["phase"]!="lobby": c.rollback(); return redirect(url_for("home"))
        p["ready"]=True
        if all(q["name"] is not None and q["ready"] for q in g["players"]):
            g["phase"]="input"; g["round"]=1
            for q in g["players"]: q["ready"]=False
        save(c,g); c.commit()
    except Exception: c.rollback(); raise
    finally: c.close()
    return redirect(url_for("home"))

@app.route("/round",methods=["GET","POST"])
@login_required
def round_page():
    g,p=current()
    if g is None: return redirect(url_for("login"))
    if g["phase"]!="input": return redirect(url_for("home"))
    if p["submitted"]: return redirect(url_for("home"))
    if request.method=="POST":
        raw=[v for v in request.form.getlist("preferences") if v.strip()]
        try: prefs=[int(v)-1 for v in raw]; bid=int(request.form.get("bid","0"))
        except ValueError:
            flash("希望とXPを正しく入力してください。","error"); return redirect(url_for("round_page"))
        if not 1<=len(prefs)<=3:
            flash("希望は1〜3個選んでください。","error"); return redirect(url_for("round_page"))
        if len(set(prefs))!=len(prefs) or any(v<0 or v>=M for v in prefs):
            flash("希望能力が重複しているか、選択が不正です。","error"); return redirect(url_for("round_page"))
        c=connect()
        try:
            c.execute("BEGIN IMMEDIATE"); g=load(c); p=get_player(g,session.get("player_id"))
            if not owns(p): c.rollback(); session.clear(); return redirect(url_for("login"))
            if g["phase"]!="input" or p["submitted"]:
                c.rollback(); flash("すでに提出済みか、入力受付が終了しました。","info"); return redirect(url_for("home"))
            if bid<0 or bid>p["state"]["xp"]:
                c.rollback(); flash(f'XPは0〜{p["state"]["xp"]}の範囲で入力してください。',"error"); return redirect(url_for("round_page"))
            p["input"]={"preferences":prefs,"bid":bid}
            p["order"]=g["next_order"]; g["next_order"]+=1; p["submitted"]=True
            if all(q["submitted"] for q in g["players"]):
                try:
                    if X == 1:
                        # 1人では隣のプレイヤーとの能力交換が定義できない。
                        # テスト用に画面遷移だけ進め、能力値・XP・割当結果は変更しない。
                        g["results"][str(g["round"])] = []
                    else:
                        states=[PlayerState(values=q["state"]["values"],used=q["state"]["used"],xp=q["state"]["xp"],
                                            total_bid_paid=q["state"]["total_bid_paid"],total_bid_refunded=q["state"]["total_bid_refunded"])
                                for q in g["players"]]
                        inputs=[PlayerInput(preferences=q["input"]["preferences"],bid=q["input"]["bid"],submitted_order=q["order"])
                                for q in g["players"]]
                        moves=solve_step(g["round"],inputs,states)
                        for i,state in enumerate(states):
                            g["players"][i]["state"]={"values":state.values,"used":state.used,"xp":state.xp,
                                "total_bid_paid":state.total_bid_paid,"total_bid_refunded":state.total_bid_refunded}
                        g["results"][str(g["round"])] = [
                            {"player":m.player,"player_name":g["players"][m.player]["name"],"ability":m.ability,
                             "score":round(float(m.score),6),"rank":m.rank,"neighbor_rank":m.neighbor_rank,
                             "v_bonus":m.v_bonus,"net_xp_cost":m.net_xp_cost,"neighbor":(m.player+1)%X}
                            for m in moves]
                    g["phase"]="results"
                except Exception as e:
                    g["phase"]="error"; g["error"]=f"{type(e).__name__}: {e}"
            save(c,g); c.commit()
        except Exception: c.rollback(); raise
        finally: c.close()
        return redirect(url_for("home"))
    return render_template("round.html",game=g,player=p)

@app.route("/results")
@login_required
def results():
    g,p=current()
    if g is None: return redirect(url_for("login"))
    if g["phase"] not in ("results","finished"): return redirect(url_for("home"))
    return render_template("results.html",game=g,player=p)

@app.post("/next-round")
@login_required
def next_round():
    c=connect()
    try:
        c.execute("BEGIN IMMEDIATE"); g=load(c); p=get_player(g,session.get("player_id"))
        if not owns(p): c.rollback(); session.clear(); return redirect(url_for("login"))
        if g["phase"]!="results": c.rollback(); return redirect(url_for("home"))
        p["ready"]=True
        if all(q["ready"] for q in g["players"]):
            if g["round"]>=N_STEPS:
                try:
                    if X != 1:
                        states=[PlayerState(values=q["state"]["values"],used=q["state"]["used"],xp=q["state"]["xp"],
                                            total_bid_paid=q["state"]["total_bid_paid"],total_bid_refunded=q["state"]["total_bid_refunded"])
                                for q in g["players"]]
                        validate_final(states)
                    # 1人テストでは最終能力値の妥当性検証を行わず、画面遷移テストとして完了させる。
                    g["phase"]="finished"
                except Exception as e:
                    g["phase"]="error"; g["error"]=f"{type(e).__name__}: {e}"
            else:
                g["round"]+=1; g["phase"]="input"; g["next_order"]=1
                for q in g["players"]:
                    q["submitted"]=False; q["input"]=None; q["order"]=None; q["ready"]=False
        save(c,g); c.commit()
    except Exception: c.rollback(); raise
    finally: c.close()
    return redirect(url_for("home"))

@app.route("/final")
@login_required
def final_results():
    g,p=current()
    if g is None: return redirect(url_for("login"))
    if g["phase"]!="finished": return redirect(url_for("home"))
    return render_template("final.html",game=g,player=p)

@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.post("/admin/reset")
def admin_reset():
    expected=os.environ.get("ADMIN_RESET_TOKEN","")
    if not expected or not secrets.compare_digest(expected,request.form.get("admin_token","")): abort(403)
    c=connect()
    try: c.execute("BEGIN IMMEDIATE"); save(c,initial_game()); c.commit()
    except Exception: c.rollback(); raise
    finally: c.close()
    return "ゲームをリセットしました。"

init_db()
if __name__=="__main__":
    app.run(debug=True)  # ローカル開発専用。本番公開時はdebug=Falseにする
