"""
tse_http_client.py — Replica a consulta de situação eleitoral do TSE via HTTP puro,
sem navegador, usando o contrato descoberto pelo probe_rede.py.

Uso:
    python tse_http_client.py <CPF>

Fluxo (mesmo do site):
  1. POST .../v1/ambiente/configuracao/consultar  (config do app)
  2. POST .../eleitor-oauth/oauth/token           (token por CPF)
  3. POST .../v5/eleitores/situacao-eleitoral     (a consulta em si)
"""

import json
import sys

import requests

BASE = "https://cad-api.tse.jus.br"
API_AUTH = "c6f7e0616edfef74aee7cde0e796d1ae"  # chave pública do app Angular (constante no JS do site)
BASIC = "YW5ndWxhcjpAbmd1bEByMA=="              # client_id/secret do app Angular (constante no JS do site)
ORIGEM = "https://www.tse.jus.br/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def conferir_configuracao(sess: requests.Session) -> None:
    r = sess.post(
        f"{BASE}/eleitor-servico/services/eleitoral/v1/ambiente/configuracao/consultar",
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "api-authorization": API_AUTH,
            "referer": ORIGEM,
            "user-agent": UA,
        },
        data='["TN3","ELO"]',
        timeout=30,
    )
    print(f"[config] {r.status_code} {r.text[:300]}")


def pegar_token(sess: requests.Session, cpf: str) -> str:
    r = sess.post(
        f"{BASE}/eleitor-oauth/oauth/token",
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "authorization": f"Basic {BASIC}",
            "api-authorization": API_AUTH,
            "referer": ORIGEM,
            "user-agent": UA,
        },
        data=f"grant_type=password&cpf={cpf}",
        timeout=30,
    )
    print(f"[token] {r.status_code} {r.text[:500]}")
    r.raise_for_status()
    return r.json()["access_token"]


def consultar_situacao(sess: requests.Session, cpf: str, token: str) -> dict:
    r = sess.post(
        f"{BASE}/eleitor-servico/services/eleitoral/v5/eleitores/situacao-eleitoral",
        headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "api-authorization": API_AUTH,
            "cpf": cpf,
            "usuario": "tn3-web",
            "sistema": "tn3-web",
            "referer": ORIGEM,
            "user-agent": UA,
        },
        data="{}",
        timeout=30,
    )
    print(f"[situacao] {r.status_code}")
    print(r.text[:3000])
    r.raise_for_status()
    return r.json()


def main() -> int:
    if len(sys.argv) < 2:
        print("Uso: python tse_http_client.py <CPF>")
        return 1
    cpf = "".join(c for c in sys.argv[1] if c.isdigit())

    sess = requests.Session()
    conferir_configuracao(sess)
    token = pegar_token(sess, cpf)
    resultado = consultar_situacao(sess, cpf, token)
    print("\n=== JSON parseado ===")
    print(json.dumps(resultado, ensure_ascii=False, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
