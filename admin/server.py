"""
admin/server.py — Dashboard admin do portal Vieira TSE.

Rodando em 127.0.0.1:8765 (só localhost). Acesso via SSH tunnel:
    ssh -L 8765:localhost:8765 -i ~/.ssh/vps-db-179 root@179.198.117.127
Aí abre http://localhost:8765/ no seu navegador.

Usa SERVICE_KEY do Supabase (bypassa RLS) — nunca deve ser exposto publicamente.
"""
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from dotenv import dotenv_values
from flask import Flask, jsonify, redirect, render_template, request, url_for
from supabase import create_client

ROOT = Path(__file__).resolve().parent.parent
env = dotenv_values(ROOT / ".env.portal")
SUPABASE_URL = (env.get("SUPABASE_URL") or "").rstrip("/")
SERVICE_KEY = env.get("SUPABASE_SERVICE_KEY") or ""
if not SUPABASE_URL or not SERVICE_KEY:
    print("Falta SUPABASE_URL/SUPABASE_SERVICE_KEY no .env.portal", file=sys.stderr)
    sys.exit(1)

sb = create_client(SUPABASE_URL, SERVICE_KEY)
app = Flask(__name__)

# ── defaults globais (espelham o systemd) ──
DEFAULT_MENSAL = 55000
DEFAULT_DIARIO = 6000


@app.template_filter("dt")
def _fmt_dt(iso):
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.strftime("%d/%m %H:%M")
    except Exception:
        return iso


def _users_map() -> dict:
    """user_id → email (via GoTrue admin)."""
    h = {"Authorization": f"Bearer {SERVICE_KEY}", "apikey": SERVICE_KEY}
    r = requests.get(f"{SUPABASE_URL}/auth/v1/admin/users", headers=h, timeout=15).json()
    return {u["id"]: u.get("email", "(sem email)") for u in r.get("users", [])}


def _mapa_batches() -> dict:
    """batch_id → {user_id, filename}."""
    lotes = sb.table("batches").select("id,user_id,filename").execute().data or []
    return {b["id"]: b for b in lotes}


@app.route("/")
def home():
    users = _users_map()
    now = datetime.now().astimezone()
    inicio_mes = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    inicio_hoje = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    mapa_b = _mapa_batches()
    # cliente → user_id (via batches)
    dono = {bid: b.get("user_id") for bid, b in mapa_b.items()}

    overrides = {
        r["user_id"]: r
        for r in (sb.table("limites_por_cliente").select("*").execute().data or [])
    }

    # consumos
    done_mes = sb.table("voter_records").select("id,batch_id").gte("checked_at", inicio_mes).execute().data or []
    err_mes = sb.table("voter_records").select("id,batch_id,updated_at").eq("status", "error").gte("updated_at", inicio_mes).execute().data or []
    done_hoje = sb.table("voter_records").select("id,batch_id").gte("checked_at", inicio_hoje).execute().data or []

    mes_por_user = defaultdict(int)
    hoje_por_user = defaultdict(int)
    for r in done_mes + err_mes:
        u = dono.get(r["batch_id"])
        if u:
            mes_por_user[u] += 1
    for r in done_hoje:
        u = dono.get(r["batch_id"])
        if u:
            hoje_por_user[u] += 1

    # fila global por status
    STATUSES = ["pending", "enriching", "ready_tse", "checking", "done", "error"]
    fila = {}
    for st in STATUSES:
        r = sb.table("voter_records").select("id", count="exact").eq("status", st).execute()
        fila[st] = r.count or 0

    clientes = []
    for uid, email in users.items():
        ov = overrides.get(uid, {})
        lim_d = ov.get("limite_diario") or DEFAULT_DIARIO
        lim_m = ov.get("limite_mensal") or DEFAULT_MENSAL
        clientes.append({
            "user_id": uid,
            "email": email,
            "consumo_hoje": hoje_por_user.get(uid, 0),
            "consumo_mes": mes_por_user.get(uid, 0),
            "limite_diario": lim_d,
            "limite_mensal": lim_m,
            "pct_mes": min(100, round((mes_por_user.get(uid, 0) / max(lim_m, 1)) * 100)),
            "override": bool(ov),
            "nota": ov.get("nota", ""),
        })
    clientes.sort(key=lambda c: -c["consumo_mes"])

    # batches recentes (10)
    lotes_all = sb.table("batches").select("id,user_id,filename,status,total,created_at").order("created_at", desc=True).limit(15).execute().data or []
    for b in lotes_all:
        b["email"] = users.get(b["user_id"], "?")
        if b.get("filename") == "__avulsas__":
            b["filename"] = "Consultas avulsas"

    return render_template("index.html", clientes=clientes, fila=fila, batches=lotes_all)


@app.route("/errors")
def errors_page():
    users = _users_map()
    mapa_b = _mapa_batches()
    regs = (sb.table("voter_records")
            .select("id,cpf,nome,status,elegibilidade,attempts,batch_id,motivo_situacao,updated_at,checked_at")
            .eq("status", "error")
            .order("updated_at", desc=True)
            .limit(200)
            .execute()).data or []
    for r in regs:
        b = mapa_b.get(r["batch_id"], {})
        r["email"] = users.get(b.get("user_id", ""), "?")
        r["filename"] = b.get("filename", "")
        if r["filename"] == "__avulsas__":
            r["filename"] = "avulsa"
    return render_template("errors.html", regs=regs)


@app.route("/incomplete")
def incomplete_page():
    users = _users_map()
    mapa_b = _mapa_batches()
    # done, mas SEM zona_eleitoral
    regs = (sb.table("voter_records")
            .select("id,cpf,nome,elegibilidade,zona_eleitoral,secao_eleitoral,municipio_votacao,local_votacao,uf,batch_id,checked_at,motivo_situacao")
            .eq("status", "done")
            .is_("zona_eleitoral", "null")
            .order("checked_at", desc=True)
            .limit(200)
            .execute()).data or []
    for r in regs:
        b = mapa_b.get(r["batch_id"], {})
        r["email"] = users.get(b.get("user_id", ""), "?")
        r["filename"] = b.get("filename", "")
        if r["filename"] == "__avulsas__":
            r["filename"] = "avulsa"
    return render_template("incomplete.html", regs=regs)


@app.route("/records/<int:rid>/edit", methods=["GET", "POST"])
def edit_record(rid):
    back = request.values.get("back", url_for("incomplete_page"))
    if request.method == "POST":
        payload = {}
        for f in ("nome", "elegibilidade", "zona_eleitoral", "secao_eleitoral",
                  "municipio_votacao", "uf", "local_votacao",
                  "endereco_votacao", "bairro_votacao"):
            v = request.form.get(f, "").strip()
            if v:
                payload[f] = v
            else:
                payload[f] = None  # permite limpar
        # remove None que veio de campos nao editados? Não — permite limpar de propósito.
        sb.table("voter_records").update(payload).eq("id", rid).execute()
        return redirect(back)
    r = sb.table("voter_records").select("*").eq("id", rid).single().execute().data
    return render_template("edit.html", reg=r, back=back)


@app.route("/records/<int:rid>/retry", methods=["POST"])
def retry_record(rid):
    """Coloca o registro de volta pra fila do TSE (ready_tse, attempts=0)."""
    sb.table("voter_records").update({
        "status": "ready_tse",
        "attempts": 0,
    }).eq("id", rid).execute()
    return redirect(request.referrer or url_for("errors_page"))


@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    # localhost-only. Nunca 0.0.0.0. (porta 8001 já em uso por outro docker na VPS)
    app.run(host="127.0.0.1", port=8765, debug=False)
