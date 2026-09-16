"""
probe_rede.py — Descobre os endpoints XHR que o Autoatendimento Eleitoral do TSE
chama por baixo do capô.

Uso:
    python probe_rede.py

O script:
  1. Abre o Chrome na página de consulta de situação do título.
  2. VOCÊ faz UMA consulta de teste com um CPF qualquer (preencha o campo e clique).
  3. Volte ao terminal e pressione Enter.
  4. Ele gera `captura_rede.json` com todos os requests XHR/fetch vistos
     (URL, método, headers, payload e corpo da resposta).

Com esse JSON a gente descobre se dá pra consultar por HTTP puro (httpx)
sem navegador — muito mais rápido e estável que clicar na página.
"""

import json
import sys

from playwright.sync_api import sync_playwright

URLS_TSE = {
    "situacao": (
        "https://www.tse.jus.br/servicos-eleitorais/"
        "autoatendimento-eleitoral#/atendimento-eleitor/consultar-situacao-titulo-eleitor"
    ),
    "titulo": (
        "https://www.tse.jus.br/servicos-eleitorais/"
        "autoatendimento-eleitoral#/atendimento-eleitor/consultar-numero-titulo-eleitor"
    ),
}

URL_TSE = URLS_TSE[sys.argv[1]] if len(sys.argv) > 1 else URLS_TSE["situacao"]

captura = []


def _headers_interessantes(headers: dict) -> dict:
    chaves = (
        "authorization", "content-type", "cookie", "x-requested-with",
        "origin", "referer", "accept",
    )
    return {k: v for k, v in headers.items() if k.lower() in chaves}


def ao_request(req, **_):
    if req.resource_type not in ("xhr", "fetch"):
        return
    captura.append({
        "url": req.url,
        "metodo": req.method,
        "resource_type": req.resource_type,
        "headers_enviados": dict(req.headers),
        "payload": req.post_data,
    })


def ao_response(res, **_):
    if res.request.resource_type not in ("xhr", "fetch"):
        return
    item = next(
        (c for c in reversed(captura) if c["url"] == res.url
         and c["metodo"] == res.request.method and "status" not in c),
        None,
    )
    if item is None:
        return
    item["status"] = res.status
    item["headers_resposta"] = _headers_interessantes(dict(res.headers))
    item["_res"] = res  # lido depois, antes de fechar o browser
    try:
        corpo = res.text()
        item["resposta"] = corpo[:5000]  # trunca respostas gigantes
    except Exception as e:  # resposta já consumida/streaming etc.
        item["resposta"] = f"<não foi possível ler: {e}>"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
        )
        page = context.new_page()
        page.on("request", ao_request)
        page.on("response", ao_response)

        print(f"Abrindo: {URL_TSE}")
        page.goto(URL_TSE, wait_until="domcontentloaded")
        print("\n>>> Faça UMA consulta de teste com um CPF na janela do Chrome.")
        print(">>> (resolva o captcha e o modal de boas-vindas se aparecer)")

        # Espera até 15 min por uma chamada à API cad-api (fora config/token),
        # depois mais 5s para assentar. Não exige interação no terminal.
        import time
        deadline = time.time() + 900
        achou = False
        while time.time() < deadline:
            time.sleep(1)
            achou = any(
                c.get("status") == 200
                and "cad-api.tse.jus.br" in c["url"]
                and "configuracao" not in c["url"]
                and "oauth" not in c["url"]
                for c in captura
            )
            if achou:
                print(">>> Chamada à API do TSE detectada. Aguardando 5s para assentar...")
                time.sleep(5)
                break

        if not achou:
            print(">>> Nada capturado em 15 min — feche o Chrome e rode de novo.")

        # Lê os corpos agora, com o browser ainda aberto (evita a corrida
        # que perdida os corpos nas rodadas anteriores).
        for c in captura:
            res = c.pop("_res", None)
            if res is not None and not c.get("resposta"):
                try:
                    c["resposta"] = res.text()[:5000]
                except Exception as e:
                    c["resposta"] = f"<não foi possível ler: {e}>"
            c.pop("_res", None)

        browser.close()

    with open("captura_rede.json", "w", encoding="utf-8") as f:
        json.dump(captura, f, ensure_ascii=False, indent=2)

    print(f"\nOK — {len(captura)} request(s) XHR/fetch capturado(s) em captura_rede.json")
    for c in captura:
        print(f"  [{c.get('status', '?')}] {c['metodo']} {c['url'][:120]}")

    if not captura:
        print("\nNenhum XHR capturado. A consulta não foi feita, ou a página "
              "navegou para outra aba/janela. Rode de novo e confira se o "
              "resultado carregou na MESMA aba.")


if __name__ == "__main__":
    sys.exit(main())
