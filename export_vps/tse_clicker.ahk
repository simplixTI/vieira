#Requires AutoHotkey v2.0
#SingleInstance Force
SetWorkingDir(A_ScriptDir)

; ============================================================================
;  tse_clicker.ahk  —  WORKER de cliques FÍSICOS pra POC do scraper TSE
; ----------------------------------------------------------------------------
;  Por que clique FÍSICO e não Selenium/JS?
;    O Angular do TSE ignora click sintético no botão "Entrar" e RESETA o form.
;    Um Click() do AutoHotkey é um evento de mouse REAL do Windows -> o Angular
;    aceita. É exatamente isso que essa POC testa.
;
;  Papel deste script: SÓ mexe na tela (navega, clica, digita, copia). Quem lê
;  o banco, parseia o resultado e grava é o Python (poc_clicker_main.py).
;  A comunicação é por arquivos na pasta bridge\.
;
;  PROTOCOLO (bridge\input.txt, 5 campos separados por '|'):
;    uuid|modo|cpf|data_br|nome_mae
;    - o UUID vem PRIMEIRO e identifica a consulta (vai em todo status.txt);
;    - a mãe vai por ÚLTIMO pra que um '|' acidental no nome não desloque
;      os demais campos (StrSplit com MaxParts=5).
;
;  CALIBRAÇÃO (faça UMA vez, com a página do TSE aberta no Chrome):
;    F8  -> liga/desliga tooltip que mostra a posição do mouse ao vivo
;    F9  -> salva a posição atual do mouse como o campo CPF
;    F10 -> salva a posição atual do mouse como o botão "Entrar"
;    (as coordenadas vão pra coords.ini ao lado deste script)
;
;  DURANTE A CONSULTA:
;    Ctrl+Shift+C -> você aperta DEPOIS de resolver o captcha, pra liberar a
;                    captura do resultado.
;    Ctrl+Shift+Q -> sai do script.
; ============================================================================

BRIDGE := A_ScriptDir "\bridge"
INI    := A_ScriptDir "\coords.ini"
URL_SITUACAO := "https://www.tse.jus.br/servicos-eleitorais/autoatendimento-eleitoral#/atendimento-eleitor/consultar-situacao-titulo-eleitor"
URL_NUMERO_TITULO := "https://www.tse.jus.br/servicos-eleitorais/autoatendimento-eleitoral#/atendimento-eleitor/consultar-numero-titulo-eleitor"
; Texto pra type-ahead do <select> de filiação (option contém "APENAS NOME DE MÃE")
FILIACAO_MATCH := "APENAS NOME DE M"
; Tempo máx esperando o resultado renderizar. Se o captcha não for resolvido
; nesse tempo, desiste -> Python mantém o registro PENDENTE e vai pro próximo.
TIMEOUT_RESULTADO_MS := 30000
; Intervalo entre as sondagens de resultado (Ctrl+A/Ctrl+C).
POLL_RESULTADO_MS := 1200

captchaDone := false
showCoords  := false

DirCreate(BRIDGE)
; Notificação NÃO-bloqueante (MsgBox travaria o loop de polling abaixo).
TrayTip("F8=ver mouse | F9/F10=CPF/Entrar (SITUAÇÃO) | Ctrl+Shift+K=calibrar TÍTULO (5 campos) | Ctrl+Shift+C=liberar captcha | Ctrl+Shift+Q=sair",
        "TSE Clicker POC pronto")

; ── Hotkeys ────────────────────────────────────────────────────────────────
F8:: {
    global showCoords
    showCoords := !showCoords
    if !showCoords
        ToolTip()
}
F9::  SaveCoord("cpf")     ; situação: campo CPF
F10:: SaveCoord("entrar")  ; situação: botão Entrar
^+k:: CalibrarTitulo()     ; calibração guiada dos 5 campos da tela de TÍTULO
^+c:: {
    global captchaDone
    captchaDone := true
    ToolTip("✔ captcha liberado — capturando resultado...")
    SetTimer(() => ToolTip(), -1500)
}
^+q:: ExitApp()

; ── Tooltip de coordenadas ao vivo ──────────────────────────────────────────
SetTimer(UpdateCoordTip, 200)
UpdateCoordTip() {
    global showCoords
    if !showCoords
        return
    MouseGetPos(&mx, &my)
    ToolTip("Mouse: " mx ", " my "`nSITUAÇÃO: F9=CPF F10=Entrar   |   TÍTULO: Ctrl+Shift+K (guiado)")
}

SaveCoord(qual) {
    global INI
    MouseGetPos(&mx, &my)
    IniWrite(mx, INI, "coords", qual "_x")
    IniWrite(my, INI, "coords", qual "_y")
    ToolTip("Salvo " qual ": " mx ", " my)
    SetTimer(() => ToolTip(), -1500)
}

; Calibração guiada da tela de TÍTULO: passa por 5 campos, ESPAÇO salva, ESC cancela.
CalibrarTitulo() {
    global INI
    campos := [["titulo_cpf",    "1) campo CPF (topo esquerda)"],
               ["titulo_drop",   "2) a CAIXA do dropdown de filiação (ponto p/ ABRIR)"],
               ["titulo_opcao",  "3) a opção APENAS NOME DE MÃE — ABRA o dropdown (clique nele) e passe o mouse na opção"],
               ["titulo_mae",    "4) campo Nome da mãe"],
               ["titulo_data",   "5) campo Data de nascimento (topo direita)"],
               ["titulo_entrar", "6) botão ENTRAR"]]
    for _, par in campos {
        chave := par[1]
        nome  := par[2]
        ; debounce: garante que o Espaço está SOLTO antes de ouvir este campo
        ; (evita que um toque duplo capture o mesmo ponto pra dois campos)
        KeyWait("Space")
        capturado := false
        while !capturado {
            MouseGetPos(&mx, &my)
            ToolTip("CALIBRAR TÍTULO (" A_Index "/6)`nMouse no " nome "`nESPAÇO = salvar   |   ESC = cancelar`nmouse: " mx ", " my)
            if GetKeyState("Escape", "P") {
                ToolTip("Calibração cancelada.")
                SetTimer(() => ToolTip(), -1200)
                return
            }
            if GetKeyState("Space", "P") {
                IniWrite(mx, INI, "coords", chave "_x")
                IniWrite(my, INI, "coords", chave "_y")
                capturado := true
                KeyWait("Space")   ; espera soltar
            }
            Sleep(40)
        }
        Sleep(150)
    }
    ToolTip("✔ Título calibrado: CPF, abrir-dropdown, opção-mãe, nome-mãe, data, Entrar.")
    SetTimer(() => ToolTip(), -2000)
}

; ── Helpers de ponte (arquivos) ─────────────────────────────────────────────
ReadStatus() {
    global BRIDGE
    try
        return Trim(FileRead(BRIDGE "\status.txt", "UTF-8"))
    catch
        return ""
}
WriteStatus(s) {
    global BRIDGE
    ; FileOpen "w" TRUNCA e regrava — evita o append-duplo de quando o Python
    ; lê o arquivo no mesmo instante (FileDelete falhava locked → FileAppend
    ; concatenava). UTF-8-RAW = sem BOM. Retry se estiver momentaneamente locked.
    Loop 12 {
        try {
            f := FileOpen(BRIDGE "\status.txt", "w", "UTF-8-RAW")
            if (f) {
                f.Write(s)
                f.Close()
                return
            }
        }
        Sleep(25)
    }
}

; ── Loop principal ──────────────────────────────────────────────────────────
; Hotkeys interrompem o Sleep, então o captchaDone é setado mesmo durante a espera.
Loop {
    st := ReadStatus()
    estado := (st = "") ? "" : StrSplit(st, "|")[1]
    if (estado = "aguardando_entrada")
        ProcessarUm()
    Sleep(700)
}

ProcessarUm() {
    global BRIDGE, INI, URL_SITUACAO, URL_NUMERO_TITULO, FILIACAO_MATCH
    global TIMEOUT_RESULTADO_MS, POLL_RESULTADO_MS, captchaDone

    raw := ""
    try raw := Trim(FileRead(BRIDGE "\input.txt", "UTF-8-RAW"))
    ; MaxParts=5: a mãe (último campo) fica intacta mesmo se contiver '|'
    parts := StrSplit(raw, "|", , 5)
    cid  := (parts.Length >= 1) ? parts[1] : ""
    modo := (parts.Length >= 2) ? parts[2] : ""
    cpf  := (parts.Length >= 3) ? parts[3] : ""
    data := (parts.Length >= 4) ? parts[4] : ""
    mae  := (parts.Length >= 5) ? parts[5] : ""
    if (cpf = "") {
        WriteStatus("erro|input sem CPF|" cid)
        return
    }
    if (modo = "")
        modo := "situacao"

    ; Coordenadas SEPARADAS por tela (o Entrar do título fica mais embaixo).
    pref := (modo = "titulo") ? "titulo_" : ""
    cpfx := IniRead(INI, "coords", pref "cpf_x", 0)
    cpfy := IniRead(INI, "coords", pref "cpf_y", 0)
    btnx := IniRead(INI, "coords", pref "entrar_x", 0)
    btny := IniRead(INI, "coords", pref "entrar_y", 0)
    if (cpfx = 0 || btnx = 0) {
        dica := (modo = "titulo") ? "Ctrl+Shift+K na tela de TÍTULO" : "F9/F10 na tela de SITUAÇÃO"
        WriteStatus("erro|coordenadas nao calibradas p/ " modo " (use " dica ")|" cid)
        return
    }

    WriteStatus("processando|" modo "|" cpf "|" cid)

    ; foca o Chrome
    if !WinExist("ahk_exe chrome.exe") {
        WriteStatus("erro|Chrome nao encontrado|" cid)
        return
    }
    WinActivate("ahk_exe chrome.exe")
    if !WinWaitActive("ahk_exe chrome.exe", , 5) {
        WriteStatus("erro|nao consegui focar o Chrome|" cid)
        return
    }
    Sleep(500)

    if (modo = "titulo") {
        ; ── Consulta NÚMERO DO TÍTULO — clica em CADA campo (6 coords) ──
        drpx := IniRead(INI, "coords", "titulo_drop_x", 0)
        drpy := IniRead(INI, "coords", "titulo_drop_y", 0)
        optx := IniRead(INI, "coords", "titulo_opcao_x", 0)
        opty := IniRead(INI, "coords", "titulo_opcao_y", 0)
        maex := IniRead(INI, "coords", "titulo_mae_x", 0)
        maey := IniRead(INI, "coords", "titulo_mae_y", 0)
        datx := IniRead(INI, "coords", "titulo_data_x", 0)
        daty := IniRead(INI, "coords", "titulo_data_y", 0)
        if (drpx = 0 || optx = 0 || maex = 0 || datx = 0) {
            WriteStatus("erro|calibre os 6 campos do titulo com Ctrl+Shift+K|" cid)
            return
        }

        Navegar(URL_NUMERO_TITULO)

        ; 1) CPF (clique FÍSICO) + digita
        Click(cpfx " " cpfy)
        Sleep(400)
        SendText(cpf)
        Sleep(300)

        ; 2) abre o dropdown de filiação
        Click(drpx " " drpy)
        Sleep(550)
        ; 3) clica na opção "APENAS NOME DE MÃE"
        Click(optx " " opty)
        Sleep(500)

        ; 4) Nome da mãe
        Click(maex " " maey)
        Sleep(300)
        Send("^a")
        Sleep(120)
        SendText(mae)
        Sleep(300)

        ; 5) Data de nascimento (dígito a dígito p/ a máscara Angular)
        Click(datx " " daty)
        Sleep(300)
        Send("^a")
        Sleep(100)
        Send("{Delete}")
        Sleep(150)
        for _, ch in StrSplit(StrReplace(data, "/", ""), "") {
            SendText(ch)
            Sleep(120)
        }
        Sleep(400)
    } else {
        ; ── Consulta SITUAÇÃO (só CPF) ──
        Navegar(URL_SITUACAO)
        Click(cpfx " " cpfy)
        Sleep(400)
        SendText(cpf)
        Sleep(400)
    }

    ; Entrar (clique FÍSICO — o Angular aceita; sintético reseta o form)
    Click(btnx " " btny)

    ; ── Aguarda o RESULTADO ─────────────────────────────────────────────────
    ; O humano resolve o captcha. O script DETECTA sozinho quando a página de
    ; resultado renderiza: a cada ~1.2s faz Ctrl+A/Ctrl+C e procura marcadores
    ; que só existem no resultado. Ctrl+Shift+C continua como override manual.
    captchaDone := false
    WriteStatus("esperando_captcha|" modo "|" cpf "|" cid)
    capturado := ""
    esperou := 0
    Loop {
        Sleep(POLL_RESULTADO_MS)
        esperou += POLL_RESULTADO_MS
        txt := CapturarPagina()
        DumpDebug(txt)               ; último texto visto, p/ debug do auto-detect
        if (txt != "" && PaginaTemResultado(txt)) {
            Sleep(1500)                  ; deixa o resto da página renderizar
            capturado := CapturarPagina()
            if (capturado = "")
                capturado := txt
            break
        }
        if (captchaDone) {               ; override manual (Ctrl+Shift+C)
            Sleep(800)
            capturado := CapturarPagina()
            break
        }
        if (esperou >= TIMEOUT_RESULTADO_MS) {
            ; captcha não resolvido a tempo -> Python mantém pendente e segue
            WriteStatus("timeout|" modo "|" cpf "|" cid)
            return
        }
    }

    if (capturado = "") {
        WriteStatus("erro|nao consegui capturar a pagina|" cid)
        return
    }
    ; escrita atômica do output (trunca + regrava), mesmo motivo do WriteStatus
    Loop 12 {
        try {
            fo := FileOpen(BRIDGE "\output.txt", "w", "UTF-8-RAW")
            if (fo) {
                fo.Write(capturado)
                fo.Close()
                break
            }
        }
        Sleep(25)
    }
    WriteStatus("resultado_capturado|" modo "|" cpf "|" cid)
}

; Seleciona tudo na página e devolve o texto do clipboard ("" se falhar).
CapturarPagina() {
    A_Clipboard := ""
    Send("^a")
    Sleep(150)
    Send("^c")
    if !ClipWait(2)
        return ""
    return A_Clipboard
}

; True se o texto tem marcador que SÓ aparece na página de resultado do TSE
; (não no formulário nem no captcha).
PaginaTemResultado(txt) {
    ; resultado positivo — situação clássica
    if RegExMatch(txt, "i)t[íi]tulo eleitoral est[áa]\s+(REGULAR|CANCELADO|SUSPENSO|TRANSFERIDO)")
        return true
    if RegExMatch(txt, "i)est[áa]\s+(CANCELADO|SUSPENSO|TRANSFERIDO|REGULAR)")
        return true
    if RegExMatch(txt, "i)t[íi]tulo\s*n[°ºo\.]?\s*[:\s]\s*\d{4}")
        return true
    ; resultado positivo — página de TÍTULO (traz zona/seção/local)
    if RegExMatch(txt, "i)zona\s*eleitoral\s*[:\s]\s*\d")
        return true
    if RegExMatch(txt, "i)se[çc][ãa]o\s*eleitoral\s*[:\s]\s*\d")
        return true
    if RegExMatch(txt, "i)local\s+de\s+vota[çc][ãa]o")
        return true
    if RegExMatch(txt, "i)situa[çc][ãa]o\s+do\s+t[íi]tulo")
        return true
    ; negativos legítimos (não localizado / divergência)
    if RegExMatch(txt, "i)(n[ãa]o foi poss[íi]vel localizar|n[ãa]o foi encontrado|diverg[êe]ncia cadastral|n[íi]vel de acesso obtido|dado divergente)")
        return true
    ; erro técnico do TSE — assenta o polling p/ o Python classificar.
    ; (NÃO inclui 'captcha'/'tente novamente', que aparecem durante a resolução)
    if RegExMatch(txt, "i)(ocorreu um erro|erro ao processar|servi[çc]o indispon[íi]vel)")
        return true
    return false
}

; Dump de debug — grava o último texto que o AHK viu, pra você inspecionar
; quando o auto-detect não bater. Sobrescreve a cada polling.
DumpDebug(txt) {
    global BRIDGE
    Loop 6 {
        try {
            fd := FileOpen(BRIDGE "\debug_ultima_captura.txt", "w", "UTF-8-RAW")
            if (fd) {
                fd.Write(txt)
                fd.Close()
                return
            }
        }
        Sleep(25)
    }
}

Navegar(url) {
    ; about:blank PRIMEIRO força reload real — navegar pra mesma URL com #hash
    ; NÃO recarrega o Angular (o resultado anterior ficaria na tela e o
    ; auto-detect capturaria resultado velho).
    Send("^l")
    Sleep(300)
    SendText("about:blank")
    Sleep(150)
    Send("{Enter}")
    Sleep(800)
    Send("^l")
    Sleep(300)
    SendText(url)
    Sleep(250)
    Send("{Enter}")
    Sleep(4500)   ; SPA Angular precisa renderizar
}
