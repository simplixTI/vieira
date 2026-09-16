"""
tse_extracao_texto.py — parser do resultado do TSE a partir de TEXTO PURO.

Porta de extrair_resultado_titulo()/extrair_resultado_situacao() (tse_extracao.py)
que exigiam WebDriver. Aqui o texto vem do clipboard capturado pelo AutoHotkey
(Ctrl+A/Ctrl+C), então parseamos só string. As chaves de saída batem com as
COLUNAS reais do banco, pra alimentar gravar_resultado() direto.
"""

import re

_ERRO_TECNICO = (
    "ocorreu um erro",
    "erro ao processar",
    "tente novamente",
    "indisponível",
    "indisponivel",
    "serviço indisponível",
)

# Colunas reais que o parser pode preencher (alinhado com tse_extracao.py).
COLUNAS_DB = (
    "motivo_situacao", "ano_situacao", "titulo_eleitoral", "zona", "secao",
    "local_votacao", "endereco_votacao", "municipio", "bairro", "pais",
    "biometria", "obrigacao_eleitoral",
)


def _apos_label(texto: str, label: str) -> str | None:
    """Pega o valor no layout 'Label\\nValor' (como o título renderiza)."""
    m = re.search(rf"{re.escape(label)}\s*\n\s*([^\n]+)", texto, re.IGNORECASE)
    if not m:
        return None
    val = m.group(1).strip()
    outros = {
        "seção", "secao", "zona", "país", "pais", "bairro", "endereço", "endereco",
        "local de votação", "município/uf", "localização",
    }
    return None if val.lower() in outros else val


def parse_resultado_texto(texto: str) -> dict:
    """
    Parseia o texto bruto (clipboard) da página de resultado — serve tanto pra
    consulta de SITUAÇÃO quanto de NÚMERO DO TÍTULO (a de título é superset).

    Retorna dict com chaves-coluna (situacao, zona, secao, titulo_eleitoral, ...)
    + chaves auxiliares: encontrado (bool), erro_tecnico (bool), texto_len (int),
    cpf_pagina (str|None, p/ anti-stale).
    """
    base = texto or ""
    low = base.lower()
    up = base.upper()

    res = {
        "situacao": None, "motivo_situacao": None, "ano_situacao": None,
        "titulo_eleitoral": None, "zona": None, "secao": None,
        "local_votacao": None, "endereco_votacao": None, "municipio": None,
        "bairro": None, "pais": None, "biometria": None, "obrigacao_eleitoral": None,
        "cpf_pagina": None, "encontrado": False, "erro_tecnico": False,
        "texto_len": len(base),
    }

    if len(base.strip()) < 40:
        res["erro_tecnico"] = True
        res["motivo_situacao"] = "texto capturado vazio/curto demais"
        return res

    # CPF na página (anti-stale)
    m_cpf = re.search(r"(\d{3}\.\d{3}\.\d{3}-\d{2})", base)
    if m_cpf:
        res["cpf_pagina"] = re.sub(r"\D", "", m_cpf.group(1))

    # ── Não localizado / divergência → resultado válido (vira regularizar_tse) ──
    if "não foi possível localizar" in low or "nao foi possivel localizar" in low:
        res["situacao"] = "NAO_CADASTRADO_TSE"
        res["motivo_situacao"] = "Dados não encontrados no TSE"
        return res
    if "nível de acesso obtido é menor" in base or "nivel de acesso obtido e menor" in low:
        res["situacao"] = "NAO_CADASTRADO_TSE"
        res["motivo_situacao"] = "Dados divergentes no cadastro"
        return res
    if ("divergência cadastral" in base or "divergencia cadastral" in low or
            "dado divergente na justiça eleitoral" in low or
            "dado divergente na justica eleitoral" in low):
        res["situacao"] = "NAO_CADASTRADO_TSE"
        res["motivo_situacao"] = "Divergência cadastral"
        return res

    # ── Situação do título ──
    if re.search(r"est[áa]\s+CANCELADO", base, re.IGNORECASE):
        res["situacao"] = "CANCELADO"
    elif re.search(r"est[áa]\s+SUSPENSO", base, re.IGNORECASE):
        res["situacao"] = "SUSPENSO"
    elif re.search(r"est[áa]\s+TRANSFERIDO", base, re.IGNORECASE):
        res["situacao"] = "TRANSFERIDO"
    elif re.search(r"t[íi]tulo eleitoral est[áa]\s+REGULAR", base, re.IGNORECASE):
        res["situacao"] = "REGULAR"
    elif re.search(r"\bREGULAR\b", base):
        res["situacao"] = "REGULAR"

    if res["situacao"]:
        res["encontrado"] = True

    m_mot = re.search(r"Motivo[:\s]+([^\n\.]{5,200})", base)
    if m_mot:
        res["motivo_situacao"] = m_mot.group(1).strip().rstrip(". ")
    else:
        sit = res["situacao"]
        if sit == "CANCELADO":
            res["motivo_situacao"] = "Título cancelado"
        elif sit == "SUSPENSO":
            res["motivo_situacao"] = "Título suspenso"
        elif sit == "TRANSFERIDO":
            res["motivo_situacao"] = "Transferido para outro domicílio"

    m_ano = re.search(r"desde\s+(\d{4})", base)
    if m_ano:
        res["ano_situacao"] = m_ano.group(1)

    # ── Número do título: "Título n°: 0530 9187 0361" ──
    m = re.search(r"T[íi]tulo n[°º\.]*[:\s]+(\d[\d\s]{10,})", base)
    if m:
        res["titulo_eleitoral"] = re.sub(r"\s+", "", m.group(1))
        res["encontrado"] = True

    # ── Campos no layout "Label\nValor" (página de título) ──
    labels = {
        "local_votacao": "Local de votação",
        "endereco_votacao": "Endereço",
        "municipio": "Município/UF",
        "bairro": "Bairro",
        "secao": "Seção",
        "pais": "País",
        "zona": "Zona",
    }
    for campo, label in labels.items():
        val = _apos_label(base, label)
        if val:
            res[campo] = val

    # Fallback inline "Zona: 0045" / "Seção: 0123"
    if not res["zona"]:
        mz = re.search(r"Zona[:\s]+(\d+)", base, re.IGNORECASE)
        if mz:
            res["zona"] = mz.group(1)
    if not res["secao"]:
        ms = re.search(r"Se[çc][ãa]o[:\s]+(\d+)", base, re.IGNORECASE)
        if ms:
            res["secao"] = ms.group(1)

    # ── Biometria ──
    if "BIOMETRIA COLETADA" in up and "NÃO COLETADA" not in up and "NAO COLETADA" not in up:
        res["biometria"] = "COLETADA"
    elif "BIOMETRIA NAO COLETADA" in up or "BIOMETRIA NÃO COLETADA" in base:
        res["biometria"] = "NAO_COLETADA"

    # ── Obrigação eleitoral ──
    if "NÃO CUMPRIMENTO DE OBRIGAÇÃO" in base or "NAO CUMPRIMENTO DE OBRIGACAO" in up:
        res["obrigacao_eleitoral"] = "EM_DEBITO"
    elif "CUMPRIMENTO DE OBRIGAÇÃO ELEITORAL" in base or "CUMPRIMENTO DE OBRIGACAO ELEITORAL" in up:
        res["obrigacao_eleitoral"] = "EM_DIA"

    # Nada encontrado + marcador de erro técnico → re-tentar
    if not res["encontrado"] and any(e in low for e in _ERRO_TECNICO):
        res["erro_tecnico"] = True
        res["motivo_situacao"] = "página de erro/captcha — re-tentar"

    return res


# Alias retrocompatível
parse_situacao_texto = parse_resultado_texto
