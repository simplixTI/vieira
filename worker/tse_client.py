"""
tse_client.py — interface de consulta ao TSE autoatendimento.

Interface pública:
    consultar_tse(cpf, nome_mae=None, data_nascimento=None) -> dict
        Devolve dict no MESMO formato de export_vps/tse_extracao_texto.py:
        parse_resultado_texto() (situacao, titulo_eleitoral, zona, secao,
        municipio, biometria, obrigacao_eleitoral, motivo_situacao,
        ano_situacao, encontrado, erro_tecnico, cpf_pagina, ...). Inclui a
        chave auxiliar "_raw" (texto bruto p/ coluna raw_text do banco).

Seleção da consulta:
    • tem nome_mae E data_nascimento → consulta ONDE VOTAR (rica: zona/seção/
      município/UF/local/endereço) via endpoint v3/onde-votar, com token de
      nível ALUMINIO_CPF (grant valida mãe+data; 401 → fallback automático
      p/ consulta de situação CPF-only, sem falhar o registro).
    • senão → consulta de SITUAÇÃO (só CPF) — endpoint v5/situacao-eleitoral.

Implementações:
    TseHttpClient — cliente HTTP real do cad-api.tse.jus.br (AMBOS os fluxos):
        1. POST /eleitor-oauth/oauth/token (password grant ligado ao CPF —
           token NUNCA é reusado entre CPFs: consulta com token de outro CPF
           retorna os dados DO OUTRO CPF silenciosamente). Com mãe+data o
           grant ganha dataNascimento (ISO) + nomeMae (maiúsculas) e sobe o
           nível de autoridade p/ ALUMINIO_CPF.
        2. POST .../v5/eleitores/situacao-eleitoral OU .../v3/eleitores/
           onde-votar (Bearer + headers cpf/usuario/sistema/ip — o header
           `ip` é OBRIGATÓRIO, sem ele 400).
        Pacing 2.5–4s com jitter ENTRE consultas de CPFs (as 2 chamadas de
        um mesmo CPF vão coladas); sessão requests.Session única (cookies);
        retry com backoff exponencial em 429/5xx/erros de rede.
    TseStubClient — textos engatilhados p/ dry-run (REGULAR c/ zona/seção,
        CANCELADO, não-localizado) → passam pelo text parser, pipeline
        completo sem rede.

main.py escolhe a implementação e registra com `usar_cliente(...)`.
"""

import json
import random
import re
import threading
import time

MODO_TITULO = "titulo"
MODO_SITUACAO = "situacao"

# ── Endpoints descobertos (probe_rede — verificados 200 OK) ──
URL_BASE = "https://cad-api.tse.jus.br"
URL_TOKEN = f"{URL_BASE}/eleitor-oauth/oauth/token"
URL_SITUACAO = f"{URL_BASE}/eleitor-servico/services/eleitoral/v5/eleitores/situacao-eleitoral"
URL_ONDE_VOTAR = f"{URL_BASE}/eleitor-servico/services/eleitoral/v3/eleitores/onde-votar"

# ── Headers fixos do contrato ──
API_AUTHORIZATION = "c6f7e0616edfef74aee7cde0e796d1ae"
AUTH_BASIC = "Basic YW5ndWxhcjpAbmd1bEByMA=="  # client angular do site do TSE
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS_COMUNS = {
    "accept": "application/json, text/plain, */*",
    "referer": "https://www.tse.jus.br/",
    "user-agent": _USER_AGENT,
}


class TseHttpError(RuntimeError):
    """Erro definitivo do HTTP do TSE (4xx fora 429, resposta inválida...)."""


class TseAuthError(TseHttpError):
    """401 no grant — mãe/data divergentes do cadastro TSE (fallback p/ CPF-only)."""


def _so_digitos(cpf: str) -> str:
    return re.sub(r"\D", "", cpf or "")


def _cpf_fmt(cpf: str) -> str:
    d = _so_digitos(cpf)
    if len(d) != 11:
        return d
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def _para_iso(data) -> "str | None":
    """
    Normaliza data p/ ISO yyyy-mm-dd (formato exigido pelo grant do TSE).
    Aceita yyyy-mm-dd, dd/mm/yyyy, dd-mm-yyyy, yyyymmdd, ddmmyyyy.
    None se não conseguir (caller faz fallback p/ CPF-only).
    """
    if not data:
        return None
    s = str(data).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{2})[/-](\d{2})[/-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    d = re.sub(r"\D", "", s)
    if len(d) == 8:
        if 1900 <= int(d[:4]) <= 2100:
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"   # yyyymmdd
        return f"{d[4:8]}-{d[2:4]}-{d[:2]}"        # ddmmyyyy
    return None


# ─────────────────────────────────────────────
# Parser JSON → shape do text parser
# ─────────────────────────────────────────────
def parse_resultado_json(dados: dict, cpf: str) -> dict:
    """
    Mapeia o JSON do endpoint situacao-eleitoral pro shape de
    parse_resultado_texto() (mesmas chaves → mesmas colunas no repo).

    Mapeamento:
        situacaoEleitoral.situacao.nome (fallback eleitor.situacao) → situacao
        eleitor.inscricao                                           → titulo_eleitoral
        mensagens[] / eleitor.temBiometria                          → biometria
        mensagens[] / eleitor.cumprimentoObrigacao                  → obrigacao_eleitoral
        situacaoEleitoral.situacao.descricao[Detalhada]             → motivo_situacao
        "desde (\\d{4})" em situacaoEleitoral.mensagem              → ano_situacao
        (a API não ecoa o CPF no body — eleitor.cpf vem null; o anti-stale
        usa o CPF que NÓS pedimos)                                  → cpf_pagina
        body sem situacao                                           → NAO_CADASTRADO_TSE
        zona/seção/município                                        → None (só no
                                                                       endpoint de título)
    """
    res = {
        "situacao": None, "motivo_situacao": None, "ano_situacao": None,
        "titulo_eleitoral": None, "zona": None, "secao": None,
        "local_votacao": None, "endereco_votacao": None, "municipio": None,
        "bairro": None, "pais": None, "biometria": None, "obrigacao_eleitoral": None,
        "nome": None, "data_nascimento": None, "uf": None,
        "cpf_pagina": _so_digitos(cpf), "encontrado": False, "erro_tecnico": False,
        "texto_len": len(json.dumps(dados, ensure_ascii=False)) if dados else 0,
        "_raw": json.dumps(dados, ensure_ascii=False) if dados else "",
    }
    if not isinstance(dados, dict):
        res["erro_tecnico"] = True
        res["motivo_situacao"] = "resposta JSON inválida/inesperada"
        return res

    eleitor = dados.get("eleitor") or {}
    sit_eleitoral = dados.get("situacaoEleitoral") or {}
    sit = sit_eleitoral.get("situacao") or {}
    mensagem = sit_eleitoral.get("mensagem") or ""
    mensagens = dados.get("mensagens") or []
    up_msgs = " ".join(str(m) for m in mensagens).upper()

    # ── Situação ──
    nome_sit = (sit.get("nome") or eleitor.get("situacao") or "").strip().upper() or None
    if nome_sit:
        res["situacao"] = nome_sit
        res["encontrado"] = True
        res["motivo_situacao"] = (sit.get("descricaoDetalhada")
                                  or sit.get("descricao")
                                  or nome_sit)
    else:
        # não-localizado / divergência → mesmo destino do text parser
        res["situacao"] = "NAO_CADASTRADO_TSE"
        res["motivo_situacao"] = "Dados não encontrados no TSE"

    # ── Título (inscrição) ──
    inscricao = (eleitor.get("inscricao") or "")
    if inscricao:
        res["titulo_eleitoral"] = re.sub(r"\D", "", str(inscricao)) or str(inscricao).strip()

    # ── Nome civil + data de nascimento (vêm no body do onde-votar) ──
    if eleitor.get("nomeCivil"):
        res["nome"] = str(eleitor["nomeCivil"]).strip()
    if eleitor.get("dataNascimento"):
        res["data_nascimento"] = _para_iso(eleitor["dataNascimento"])

    # ── Local de votação (onde-votar): prefere ondeVotara, fallback domicílio ──
    local = dados.get("ondeVotara") or dados.get("domicilioEleitoral") or {}
    if local:
        res["zona"] = local.get("zona")
        res["secao"] = local.get("secao")
        res["local_votacao"] = local.get("nome")
        res["endereco_votacao"] = local.get("endereco")
        res["bairro"] = local.get("bairro")
        res["municipio"] = local.get("municipio")
        res["uf"] = local.get("uf")

    # ── Biometria: mensagens têm precedência sobre o boolean ──
    if "BIOMETRIA COLETADA" in up_msgs:
        res["biometria"] = "COLETADA"
    elif "BIOMETRIA" in up_msgs and ("NÃO COLETADA" in up_msgs or "NAO COLETADA" in up_msgs):
        res["biometria"] = "NAO_COLETADA"
    elif eleitor.get("temBiometria") is True:
        res["biometria"] = "COLETADA"
    elif eleitor.get("temBiometria") is False:
        res["biometria"] = "NAO_COLETADA"

    # ── Obrigação eleitoral ──
    if ("NÃO CUMPRIMENTO DE OBRIGAÇÃO" in up_msgs
            or "NAO CUMPRIMENTO DE OBRIGACAO" in up_msgs):
        res["obrigacao_eleitoral"] = "EM_DEBITO"
    elif "CUMPRIMENTO DE OBRIGAÇÃO" in up_msgs or "CUMPRIMENTO DE OBRIGACAO" in up_msgs:
        res["obrigacao_eleitoral"] = "EM_DIA"
    elif eleitor.get("cumprimentoObrigacao") is True:
        res["obrigacao_eleitoral"] = "EM_DIA"
    elif eleitor.get("cumprimentoObrigacao") is False:
        res["obrigacao_eleitoral"] = "EM_DEBITO"

    # ── Ano da situação ("está REGULAR desde 2020") ──
    m_ano = re.search(r"desde\s+(\d{4})", mensagem)
    if m_ano:
        res["ano_situacao"] = m_ano.group(1)

    return res


class TseClientBase:
    """Seleciona o modo de consulta e delega à subclasse."""

    #: True = o próprio cliente faz pacing entre consultas (TseHttpClient).
    #: False = quem chama faz pacing (main.py paces o stub no dry-run).
    pacing_interno = True
    #: True = consultar_titulo() implementada (zona/seção/município).
    #: False = main.py loga 1x e faz fallback p/ consulta de situação.
    tem_titulo = False

    def consultar_tse(self, cpf: str, nome_mae: str = None,
                      data_nascimento: str = None) -> "dict | str":
        cpf_limpo = _so_digitos(cpf)
        if len(cpf_limpo) != 11:
            raise ValueError(f"CPF inválido: {cpf!r}")
        modo = (MODO_TITULO if (nome_mae and data_nascimento)
                else MODO_SITUACAO)
        return self._consultar(modo, cpf_limpo, nome_mae, data_nascimento)

    def consultar_titulo(self, cpf: str, data_nascimento: str,
                         nome_mae: str) -> dict:
        """
        Consulta rica de NÚMERO DO TÍTULO (zona/seção/município/local de votação).

        Só existe no TseHttpClient (o stub não tem esse modo).
        """
        raise NotImplementedError("consultar_titulo só existe no TseHttpClient")

    def _consultar(self, modo: str, cpf: str, nome_mae: "str | None",
                   data_nascimento: "str | None") -> "dict | str":
        raise NotImplementedError


class TseHttpClient(TseClientBase):
    """
    Cliente HTTP real do cad-api.tse.jus.br.

    Dois fluxos por CPF (2 chamadas COLADAS em cada, sem sleep entre elas —
    o pacing 2.5–4s acontece ENTRE consultas de CPFs diferentes):

    SITUAÇÃO (CPF-only, token nível FERRO/ESTANHO):
        1. POST /eleitor-oauth/oauth/token — password grant LIGADO AO CPF
           (body: grant_type=password&cpf=...).
           ⚠️ NUNCA reusar token entre CPFs: a API identifica pelo token e
           ignora o header cpf — token de A aplicado a B retorna dados de A.
        2. POST .../v5/eleitores/situacao-eleitoral — Bearer + headers
           cpf/usuario/sistema/ip (`ip` é OBRIGATÓRIO; sem ele → 400).

    TÍTULO / ONDE VOTAR (CPF + mãe + data, token nível ALUMINIO_CPF):
        1. Mesmo grant, com dataNascimento (ISO yyyy-mm-dd) + nomeMae
           (maiúsculas, como vem do enriquecimento). O grant VALIDA mãe+data
           contra o cadastro — divergência → 401 → fallback automático p/
           fluxo SITUAÇÃO (CPF-only), sem falhar o registro.
        2. POST .../v3/eleitores/onde-votar — traz zona/seção/município/UF/
           local/endereço/bairro (prefere ondeVotara, fallback domicilioEleitoral)
           + nomeCivil/dataNascimento + as mesmas situação/mensagens.

    Sessão requests.Session única (reusa cookies de sessão do TSE), retry
    com backoff exponencial em 429/5xx/erros de rede, timeout configurável.
    """

    pacing_interno = True
    tem_titulo = True

    def __init__(self, timeout: float = 30.0, tentativas: int = 4,
                 pacing: float = 2.5, jitter_max: float = 4.0,
                 sessao: "object | None" = None):
        self.timeout = timeout
        self.tentativas = tentativas
        self.pacing = pacing
        self.jitter_max = jitter_max
        self._sessao = sessao
        self._ultima_req = 0.0
        self._lock = threading.Lock()  # pacing é global, não por thread

    # ── Sessão reusada (cookies do TSE) ──
    @property
    def sessao(self):
        if self._sessao is None:
            import requests  # import local: deixa o stub rodar sem requests
            self._sessao = requests.Session()
        return self._sessao

    # ── Pacing anti-ban: 2.5–4s entre CPFs (jitter aleatório) ──
    def _respeitar_pacing(self) -> None:
        with self._lock:
            agora = time.monotonic()
            espera = self.pacing + random.uniform(0.0, self.jitter_max - self.pacing)
            falta = espera - (agora - self._ultima_req)
            if falta > 0:
                time.sleep(falta)
            self._ultima_req = time.monotonic()

    # ── Headers por tipo de chamada (contrato descoberto) ──
    def _headers_token(self) -> dict:
        return {
            **_HEADERS_COMUNS,
            "authorization": AUTH_BASIC,
            "api-authorization": API_AUTHORIZATION,
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        }

    def _headers_consulta(self, token: str, cpf: str) -> dict:
        return {
            **_HEADERS_COMUNS,
            "authorization": f"Bearer {token}",
            "api-authorization": API_AUTHORIZATION,
            "cpf": cpf,
            "usuario": "tn3-web",
            "sistema": "tn3-web",
            "ip": "1.1.1.1",  # ⚠️ MANDATÓRIO — sem este header a API retorna 400
            "content-type": "application/json",
        }

    # ── Token: 1 por CPF, nunca reusado entre CPFs ──
    # Com mãe+data o grant sobe o nível de auth p/ ALUMINIO_CPF (acesso ao
    # onde-votar). 401 aqui = mãe/data divergentes do cadastro TSE.
    def _obter_token(self, cpf: str, data_nascimento: "str | None" = None,
                     nome_mae: "str | None" = None) -> str:
        data = {"grant_type": "password", "cpf": cpf}
        if nome_mae and data_nascimento:
            data["dataNascimento"] = data_nascimento
            data["nomeMae"] = nome_mae.strip().upper()
        try:
            r = self._request_com_retry(
                "POST", URL_TOKEN,
                headers=self._headers_token(),
                data=data,
            )
        except TseHttpError as e:
            if "401" in str(e):
                raise TseAuthError(str(e))
            raise
        try:
            token = r.json().get("access_token")
        except Exception:
            raise TseHttpError(f"TSE: resposta do token não-JSON: {(r.text or '')[:200]}")
        if not token:
            raise TseHttpError("TSE: token sem access_token (credenciais/headers?)")
        return token

    # ── Consulta de situação (CPF-only): token CPF + situacao-eleitoral ──
    # 400 com body vazio/`[]` = CPF sem registro no TSE (observado no piloto):
    # é um resultado VÁLIDO (não-cadastrado), não um erro técnico.
    def _consultar_situacao(self, cpf: str) -> dict:
        token = self._obter_token(cpf)
        r = self._request_com_retry(
            "POST", URL_SITUACAO,
            headers=self._headers_consulta(token, cpf),
            json={},
            aceitar_400_vazio=True,
        )
        if r.status_code == 400:
            return parse_resultado_json({}, cpf)  # NAO_CADASTRADO_TSE, encontrado=False
        try:
            dados = r.json()
        except Exception:
            raise TseHttpError(f"TSE: resposta da situação não-JSON: {(r.text or '')[:200]}")
        # headers da resposta carregam codigo/mensagem (ex.: codigo=2 = sucesso);
        # o parse abaixo decide encontrado/não-cadastrado pelo body.
        return parse_resultado_json(dados, cpf)

    # ── Consulta rica (título/onde votar): precisa de mãe+data ──
    def consultar_titulo(self, cpf: str, data_nascimento: str,
                         nome_mae: str) -> dict:
        """
        Onde votar (zona/seção/município/local) via token ALUMINIO_CPF.
        Fallback automático p/ consulta de situação (CPF-only) quando:
          • faltam mãe/data (ou data não parseia) — nem tenta o grant rico;
          • o grant rejeita mãe+data com 401 (dados divergentes do TSE);
          • o onde-votar responde 403 "Acesso negado" (observado p/ alguns
            CPFs, provavelmente CANCELADO/SUSPENSO/TRANSFERIDO cujo local de
            votação não é liberado) — situação/título/biometria vêm do CPF-only;
            só zona/seção/município ficam em branco (fiel à fonte).
        """
        self._respeitar_pacing()  # 1 pacing por CPF, aqui (fallback não re-pacing)
        data_iso = _para_iso(data_nascimento)
        mae = (nome_mae or "").strip() or None
        if not (mae and data_iso):
            print(f"      ⤵️  sem mãe/data válida — usando consulta de SITUAÇÃO "
                  f"(CPF-only) pra CPF …{cpf[-4:]}")
            return self._consultar_situacao(cpf)
        try:
            token = self._obter_token(cpf, data_iso, mae)
        except TseAuthError:
            # mãe/data divergentes do cadastro TSE → não falha o registro:
            # cai no CPF-only (mesmo custo: token + 1 consulta)
            print(f"      ⤵️  mãe/data rejeitadas pelo TSE (401) — fallback p/ "
                  f"SITUAÇÃO (CPF-only), CPF …{cpf[-4:]}")
            return self._consultar_situacao(cpf)
        try:
            r = self._request_com_retry(
                "POST", URL_ONDE_VOTAR,
                headers=self._headers_consulta(token, cpf),
                json={},
                aceitar_400_vazio=True,
            )
        except TseHttpError as e:
            if "403" in str(e):
                # local de votação não liberado p/ este CPF → CPF-only
                self._avisar_403()
                return self._consultar_situacao(cpf)
            raise
        if r.status_code == 400:
            return parse_resultado_json({}, cpf)  # sem registro no TSE
        try:
            dados = r.json()
        except Exception:
            raise TseHttpError(f"TSE: resposta do onde-votar não-JSON: {(r.text or '')[:200]}")
        return parse_resultado_json(dados, cpf)

    #: contador p/ logar o 403 do onde-votar só na 1ª vez (depois em silêncio)
    _contagem_403 = 0

    def _avisar_403(self) -> None:
        TseHttpClient._contagem_403 += 1
        if TseHttpClient._contagem_403 == 1:
            print("      ⤵️  onde-votar respondeu 403 'Acesso negado' — usando "
                  "SITUAÇÃO (CPF-only) p/ estes CPFs (fica sem zona/seção). "
                  "(avisado 1x; próximos 403 são contados em silêncio)")

    # ── Fluxo de consulta (dispatcher de modo) ──
    def _consultar(self, modo: str, cpf: str, nome_mae: "str | None",
                   data_nascimento: "str | None") -> dict:
        if modo == MODO_TITULO:
            return self.consultar_titulo(cpf, data_nascimento or "", nome_mae or "")
        self._respeitar_pacing()  # pacing ENTRE CPFs; as 2 chamadas abaixo são coladas
        return self._consultar_situacao(cpf)

    # ── request com retry exponencial em 429/5xx/rede/JSON inválido ──
    # aceitar_400_vazio: 400 com body vazio/`[]` = CPF sem registro no TSE
    # (observado no piloto) — o CALLER trata como não-cadastrado, não erro.
    def _request_com_retry(self, method: str, url: str, headers: dict,
                           params: dict = None, json: dict = None,
                           data: dict = None, aceitar_400_vazio: bool = False):
        backoff = 2.0
        ultima_exc: "Exception | None" = None
        for tentativa in range(1, self.tentativas + 1):
            try:
                r = self.sessao.request(method, url, headers=headers,
                                        params=params, json=json, data=data,
                                        timeout=self.timeout)
            except Exception as e:
                # timeout/conexão: transitório → retry
                ultima_exc = e
                if tentativa < self.tentativas:
                    time.sleep(backoff + random.uniform(0, 1))
                    backoff *= 2
                    continue
                raise TseHttpError(f"TSE: falha de rede após {tentativa} tentativas: {e}")
            if r.status_code == 429 or r.status_code >= 500:
                if tentativa < self.tentativas:
                    time.sleep(backoff + random.uniform(0, 1))
                    backoff *= 2
                    continue
                raise TseHttpError(f"TSE: HTTP {r.status_code} persistiu após "
                                   f"{tentativa} tentativas")
            if r.status_code == 400 and aceitar_400_vazio \
                    and (r.text or "").strip() in ("", "[]"):
                return r  # caller decide: não-cadastrado (sem registro no TSE)
            if r.status_code != 200:
                # 401 NÃO é retentado: token é emitido na hora, então 401 é
                # definitivo (grant rejeitou mãe+data / credenciais). Caller
                # decide (ex.: consultar_titulo faz fallback p/ CPF-only).
                raise TseHttpError(
                    f"TSE: HTTP {r.status_code} em {url} (erro definitivo): "
                    f"{(getattr(r, 'text', '') or '')[:200]}")
            if not (r.text or "").strip():
                ultima_exc = TseHttpError("TSE: resposta vazia")
                if tentativa < self.tentativas:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise ultima_exc
            return r
        raise ultima_exc or TseHttpError("TSE: falha inesperada")


class TseStubClient(TseClientBase):
    """
    Devolve textos engatilhados compatíveis com parse_resultado_texto(),
    pra testar o pipeline ponta-a-ponta SEM rede. O CPF pedido vai embutido
    no texto (pra passar no anti-stale cpf_pagina == cpf).
    """

    pacing_interno = False  # main.py faz o pacing no dry-run
    tem_titulo = False

    def _consultar(self, modo: str, cpf: str, nome_mae: "str | None",
                   data_nascimento: "str | None") -> str:
        if modo == MODO_TITULO:
            return self._texto_regular(cpf)
        # modo situação: alterna CANCELADO / não-localizado pelo último dígito
        if int(cpf[-1]) % 2 == 0:
            return self._texto_cancelado(cpf)
        return self._texto_nao_localizado(cpf)

    @staticmethod
    def _texto_regular(cpf: str) -> str:
        return f"""Consulta ao TSE — Autoatendimento do Eleitor
CPF: {_cpf_fmt(cpf)}
Nome: ELEITOR DE TESTE STUB
Situação do título
O título eleitoral está REGULAR desde 2020.
Motivo: Quite com as obrigações eleitorais
Título n°: 0530 9187 0361
Local de votação
ESCOLA MUNICIPAL STUB DE TESTE
Endereço
RUA DOS BOBOs, 123 — CENTRO
Município/UF
FORTALEZA/CE
Bairro
CENTRO
Zona
0045
Seção
0123
País
BRASIL
BIOMETRIA COLETADA
CUMPRIMENTO DE OBRIGAÇÃO ELEITORAL
"""

    @staticmethod
    def _texto_cancelado(cpf: str) -> str:
        return f"""Consulta ao TSE — Autoatendimento do Eleitor
CPF: {_cpf_fmt(cpf)}
Situação do título
O título eleitoral está CANCELADO desde 2019.
Motivo: Ausência de votação justificada
Nome: ELEITOR CANCELADO STUB
Para regularizar, procure o cartório eleitoral.
"""

    @staticmethod
    def _texto_nao_localizado(cpf: str) -> str:
        return f"""Consulta ao TSE — Autoatendimento do Eleitor
CPF: {_cpf_fmt(cpf)}
Não foi possível localizar os dados do eleitor com as informações informadas.
Verifique os dados e tente novamente.
"""


# ─────────────────────────────────────────────
# Cliente padrão do processo (main.py registra o real ou o stub)
# ─────────────────────────────────────────────
_cliente: "TseClientBase | None" = None


def usar_cliente(c: TseClientBase) -> None:
    global _cliente
    _cliente = c


def cliente() -> TseClientBase:
    if _cliente is None:
        raise RuntimeError("Nenhum cliente TSE registrado — chame usar_cliente() "
                           "(main.py faz isso; use --stub p/ dry-run).")
    return _cliente


def consultar_tse(cpf: str, nome_mae: str = None,
                  data_nascimento: str = None) -> dict:
    """
    Interface do worker: dict no shape do parser (pronto pro repo.gravar_*).

    Clientes podem devolver str (texto bruto — passa por
    export_vps/tse_extracao_texto.py:parse_resultado_texto; caminho mantido
    como fallback futuro) ou dict já parseado (modo JSON do HTTP real).
    Sempre inclui "_raw" (texto bruto p/ coluna raw_text).
    """
    bruto = cliente().consultar_tse(cpf, nome_mae=nome_mae,
                                    data_nascimento=data_nascimento)
    if isinstance(bruto, str):
        from tse_extracao_texto import parse_resultado_texto  # lazy: precisa do path do export_vps
        res = parse_resultado_texto(bruto)
        res["_raw"] = bruto
        return res
    return bruto
