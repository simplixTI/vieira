"""
pega_js_tse.py — Abre o autoatendimento com Playwright, espera os chunks
lazy-load da tela de consulta, baixa TODOS os JS (inclusive chunks) pelo
próprio browser e procura o código que monta o payload das consultas
(local-votacao, situacao-eleitoral, pleito).
"""

import re

from playwright.sync_api import sync_playwright

URL_TSE = (
    "https://www.tse.jus.br/servicos-eleitorais/"
    "autoatendimento-eleitoral#/atendimento-eleitor/consultar-numero-titulo-eleitor"
)

ALVOS = ("eleitores/", "pleito")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        ctx = browser.new_context(viewport={"width": 1366, "height": 900},
                                  locale="pt-BR")
        page = ctx.new_page()
        page.goto(URL_TSE, wait_until="networkidle")
        # interage com a tela para forçar o carregamento dos chunks lazy
        page.wait_for_timeout(4000)
        page.keyboard.press("Tab")
        page.wait_for_timeout(2000)
        page.goto(URL_TSE.replace("numero-titulo", "situacao-titulo"),
                  wait_until="networkidle")
        page.wait_for_timeout(4000)

        recursos = page.evaluate(
            "performance.getEntriesByType('resource')"
            ".map(e => e.name).filter(n => n.includes('.js'))"
        )
        js_urls = sorted(set(recursos))
        print(f"{len(js_urls)} JS (incluindo chunks lazy)")

        achados = []
        for url in js_urls:
            try:
                texto = ctx.request.get(url).text()
            except Exception as e:
                print(f"  falhou {url[-80:]}: {str(e)[:60]}")
                continue
            if "local-votacao" not in texto and "situacao-eleitoral" not in texto:
                continue
            print(f"  >>> HIT: {url[-90:]} ({len(texto)} bytes)")
            for alvo in ALVOS:
                for m in re.finditer(re.escape(alvo), texto):
                    ini = max(0, m.start() - 1000)
                    fim = min(len(texto), m.end() + 1200)
                    achados.append((url.split("/")[-1][:60], alvo, texto[ini:fim]))

        browser.close()

    with open("trechos_js.txt", "w", encoding="utf-8") as f:
        for nome, alvo, trecho in achados:
            f.write("=" * 100 + "\n")
            f.write(f"ARQUIVO: {nome} | ALVO: {alvo}\n")
            f.write(trecho + "\n\n")
    print(f"{len(achados)} trechos salvos em trechos_js.txt")


if __name__ == "__main__":
    main()
