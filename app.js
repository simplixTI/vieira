/* Portal de Verificação Eleitoral — lógica principal (vanilla JS, sem build). */
(function () {
  'use strict';

  var MAX_CPFS = 40000;
  var CHUNK_SIZE = 500;
  var PAGE_SIZE = 250;
  var EXPORT_CHUNK = 1000;
  var AUTO_REFRESH_MS = 20000;
  var AVULSA_POLL_MS = 5000;             // polling rápido enquanto tem CPF avulso pendente
  var AVULSA_FILENAME = '__avulsas__';   // marca do lote no banco (mesma no worker)
  var AVULSA_LABEL = 'Consultas avulsas';

  function hasCelular() {
    var cfg = window.PORTAL_CONFIG || {};
    return !!(cfg.FEATURES && cfg.FEATURES.celular);
  }

  // ---------- estado ----------
  var state = {
    client: null,
    session: null,
    pendingFile: null,       // { filename, records: [{cpf, nome}], total, rejected: [{cpf, motivo}] }
    batches: [],
    currentBatch: null,      // batch selecionado
    page: 0,
    autoRefreshTimer: null,
    exporting: false,
    avulsaBatchId: null,     // lote persistente 'Consultas avulsas' do usuário
    avulsaWatch: null,       // { recordId, cpf, timer } enquanto aguarda resultado
  };

  // ---------- helpers de DOM ----------
  function $(sel) { return document.querySelector(sel); }
  function show(el) { el.hidden = false; }
  function hide(el) { el.hidden = true; }
  function setMsg(el, text) { el.textContent = text || ''; }

  function showView(name) {
    ['login', 'upload', 'dashboard'].forEach(function (v) {
      $('#view-' + v).hidden = (v !== name);
    });
  }

  function formatCPF(cpf) {
    return cpf.replace(/(\d{3})(\d{3})(\d{3})(\d{2})/, '$1.$2.$3-$4');
  }

  function formatDateTime(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString('pt-BR');
  }

  function batchLabel(b) {
    if (!b) return '';
    if ((b.filename || '') === AVULSA_FILENAME) return AVULSA_LABEL;
    return (b.filename || '(sem nome)') + ' — ' + formatDateTime(b.created_at);
  }

  function isAvulsaBatch(b) {
    return !!(b && (b.filename || '') === AVULSA_FILENAME);
  }

  // ---------- CPFs já consultados (dedupe por cliente) ----------
  // Um CPF já consultado nunca mais é consultado para o mesmo cliente: repetir
  // custaria 1 chamada de API paga (fase A) + 1 consulta da cota (fase B) e criaria
  // uma segunda linha do mesmo CPF — dois históricos e duplicata no CSV exportado.
  // A garantia dura é do banco (índice único user_id+cpf + trigger); o que segue
  // existe para o cliente SABER por que o CPF não entrou, em vez de sumir calado.
  // Exceção: CPF em 'error' definitivo não bloqueia — o cliente não recebeu nada,
  // e ao reenviar o banco reaproveita a linha original em vez de duplicar.

  var LOOKUP_CHUNK = 500;   // ~6 KB de URL; blocos maiores o PostgREST recusa

  function buscarJaConsultados(cpfs, onProgress) {
    var achados = {};
    if (!cpfs.length) return Promise.resolve(achados);
    var chain = Promise.resolve();
    var feitos = 0;
    for (var i = 0; i < cpfs.length; i += LOOKUP_CHUNK) {
      (function (slice) {
        chain = chain.then(function () {
          // RLS já limita aos registros do próprio cliente — não precisa filtrar por user_id
          return state.client
            .from('voter_records')
            .select('id,cpf,status,checked_at,batch_id,elegibilidade,zona_eleitoral,secao_eleitoral,municipio_votacao')
            .in('cpf', slice)
            .then(function (res) {
              if (res.error) throw res.error;
              (res.data || []).forEach(function (r) { achados[r.cpf] = r; });
              feitos += slice.length;
              if (onProgress) onProgress(Math.min(feitos, cpfs.length), cpfs.length);
            });
        });
      })(cpfs.slice(i, i + LOOKUP_CHUNK));
    }
    return chain.then(function () { return achados; });
  }

  function jaFoiConsultado(reg) {
    return !!reg && reg.status !== 'error';
  }

  // Lotes só para rotular a mensagem ("lote fulano.xlsx"). Consulta própria porque
  // a tela de upload pode ser aberta antes do dashboard carregar state.batches.
  function lotesParaRotulo() {
    if (state.batches && state.batches.length) return Promise.resolve(state.batches);
    return state.client
      .from('batches')
      .select('id,filename,created_at')
      .then(function (res) { return res.error ? [] : (res.data || []); });
  }

  function motivoJaConsultado(reg, lotes) {
    var lote = (lotes || []).find(function (b) { return b.id === reg.batch_id; });
    var ondeTxt = lote ? ' (lote ' + batchLabel(lote) + ')' : '';
    if (reg.status !== 'done') {
      return 'Já está na fila de consulta' + ondeTxt;
    }
    var quando = reg.checked_at ? formatDateTime(reg.checked_at) : '';
    return 'Já consultado' + (quando ? ' em ' + quando : '') + ondeTxt;
  }

  // ---------- validação de CPF ----------
  function cpfDigit(digits, multipliers) {
    var sum = 0;
    for (var i = 0; i < multipliers.length; i++) sum += digits[i] * multipliers[i];
    var rest = sum % 11;
    return rest < 2 ? 0 : 11 - rest;
  }

  function validateCPF(cpf) {
    // cpf: string de 11 dígitos
    var allSame = true;
    for (var i = 1; i < 11; i++) {
      if (cpf[i] !== cpf[0]) { allSame = false; break; }
    }
    if (allSame) return false;
    var d = [];
    for (var j = 0; j < 11; j++) d.push(Number(cpf[j]));
    var m1 = [10, 9, 8, 7, 6, 5, 4, 3, 2];
    var m2 = [11, 10, 9, 8, 7, 6, 5, 4, 3, 2];
    if (cpfDigit(d.slice(0, 9), m1) !== d[9]) return false;
    if (cpfDigit(d.slice(0, 10), m2) !== d[10]) return false;
    return true;
  }

  function normalizeCPF(raw) {
    return String(raw == null ? '' : raw).replace(/\D/g, '');
  }

  // ---------- boot / config ----------
  function boot() {
    var cfg = window.PORTAL_CONFIG;
    if (!cfg || !cfg.SUPABASE_URL || !cfg.SUPABASE_ANON_KEY || cfg.SUPABASE_ANON_KEY === 'PREENCHER_ANON_KEY') {
      show($('#config-error'));
      return;
    }
    if (typeof window.supabase === 'undefined') {
      show($('#config-error'));
      $('#config-error').querySelector('p').textContent =
        'Não foi possível carregar a biblioteca do Supabase. Verifique sua conexão com a internet e recarregue a página.';
      return;
    }

    state.client = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
    show($('#app'));

    // Mostra os elementos [data-feature="celular"] se a feature está habilitada.
    if (hasCelular()) {
      Array.prototype.forEach.call(
        document.querySelectorAll('[data-feature="celular"]'),
        function (el) { el.hidden = false; }
      );
    }

    bindAuth();
    bindUpload();
    bindDashboard();
    bindAvulsa();

    state.client.auth.getSession().then(function (res) {
      if (res.error) {
        showView('login');
        return;
      }
      state.session = res.data.session;
      if (state.session) {
        showView('dashboard');
        loadBatches();
      } else {
        showView('login');
      }
    });
  }

  function handleAuthError(error) {
    // sessão expirada / token inválido → volta ao login
    var msg = (error && error.message) || '';
    if (/session|token|jwt|auth|expired/i.test(msg)) {
      state.session = null;
      state.batches = [];
      state.currentBatch = null;
      state.avulsaBatchId = null;
      stopAutoRefresh();
      stopAvulsaWatch();
      showView('login');
      setMsg($('#login-error'), 'Sua sessão expirou. Faça login novamente.');
      return true;
    }
    return false;
  }

  // ---------- AUTH ----------
  function bindAuth() {
    state.client.auth.onAuthStateChange(function (_event, session) {
      state.session = session;
      if (session) {
        showView('dashboard');
        state.page = 0;
        loadBatches();
      } else {
        stopAutoRefresh();
        stopAvulsaWatch();
        state.avulsaBatchId = null;
        state.batches = [];
        state.currentBatch = null;
        showView('login');
      }
    });

    $('#form-login').addEventListener('submit', function (ev) {
      ev.preventDefault();
      var email = $('#login-email').value.trim();
      var password = $('#login-password').value;
      var btn = $('#btn-login');
      btn.disabled = true;
      btn.textContent = 'Entrando…';
      setMsg($('#login-error'), '');
      state.client.auth.signInWithPassword({ email: email, password: password })
        .then(function (res) {
          if (res.error) {
            setMsg($('#login-error'), 'Não foi possível entrar: ' + res.error.message);
            return;
          }
          // onAuthStateChange cuida da troca de view
        })
        .catch(function () {
          setMsg($('#login-error'), 'Falha de rede. Tente novamente.');
        })
        .finally(function () {
          btn.disabled = false;
          btn.textContent = 'Entrar';
        });
    });

    function doLogout() {
      state.client.auth.signOut().then(function () {
        stopAutoRefresh();
        stopAvulsaWatch();
        state.avulsaBatchId = null;
        setAvulsaMsg('', '');
        if ($('#avulsa-cpf')) $('#avulsa-cpf').value = '';
        resetUploadView();
        resetDashboardView();
      });
    }
    $('#btn-logout-upload').addEventListener('click', doLogout);
    $('#btn-logout-dashboard').addEventListener('click', doLogout);
  }

  // ---------- UPLOAD ----------
  function bindUpload() {
    $('#btn-go-dashboard').addEventListener('click', function () {
      showView('dashboard');
      if (!state.batches.length) loadBatches();
    });
    $('#file-input').addEventListener('change', onFileSelected);
    $('#btn-confirm-upload').addEventListener('click', confirmUpload);
  }

  function resetUploadView() {
    $('#file-input').value = '';
    hide($('#upload-summary'));
    hide($('#upload-progress'));
    hide($('#rejections-wrap'));
    hide($('#repetidos-wrap'));
    $('#btn-confirm-upload').hidden = false;
    $('#btn-confirm-upload').disabled = false;
    setMsg($('#upload-error'), '');
    state.pendingFile = null;
  }

  function onFileSelected(ev) {
    var file = ev.target.files[0];
    resetUploadView();
    if (!file) return;

    setMsg($('#upload-error'), 'Lendo arquivo…');
    var parsed = null;
    readFileAsRows(file)
      .then(function (rows) {
        parsed = parseRows(rows);
        if (!parsed.records.length) return { achados: {}, lotes: [] };
        // Antes de aceitar, descobre quais desses CPFs o cliente já consultou.
        setMsg($('#upload-error'), 'Verificando CPFs já consultados…');
        return lotesParaRotulo().then(function (lotes) {
          var cpfs = parsed.records.map(function (r) { return r.cpf; });
          return buscarJaConsultados(cpfs, function (feitos, total) {
            if (total > LOOKUP_CHUNK) {
              setMsg($('#upload-error'), 'Verificando CPFs já consultados… ' + feitos + ' de ' + total);
            }
          }).then(function (achados) { return { achados: achados, lotes: lotes }; });
        });
      })
      .then(function (ctx) {
        setMsg($('#upload-error'), '');

        var aceitos = [];
        var repetidos = [];
        parsed.records.forEach(function (rec) {
          var reg = ctx.achados[rec.cpf];
          if (jaFoiConsultado(reg)) {
            repetidos.push({ cpf: rec.cpf, motivo: motivoJaConsultado(reg, ctx.lotes) });
          } else {
            aceitos.push(rec);
          }
        });

        state.pendingFile = {
          filename: file.name,
          records: aceitos,
          total: parsed.records.length + parsed.rejected.length,
          rejected: parsed.rejected,
          repetidos: repetidos,
        };

        $('#stat-total').textContent = state.pendingFile.total;
        $('#stat-aceitos').textContent = aceitos.length;
        $('#stat-rejeitados').textContent = parsed.rejected.length;
        $('#stat-repetidos').textContent = repetidos.length;

        preencherLista($('#rejections-list'), parsed.rejected);
        $('#rejections-wrap').hidden = !parsed.rejected.length;
        preencherLista($('#repetidos-list'), repetidos);
        $('#repetidos-wrap').hidden = !repetidos.length;

        // Só faz sentido confirmar se sobrou algo novo pra consultar.
        $('#btn-confirm-upload').hidden = aceitos.length === 0;

        if (aceitos.length === 0) {
          setMsg($('#upload-error'), repetidos.length
            ? 'Todos os CPFs válidos deste arquivo já foram consultados antes — nada a enviar. Os resultados estão no dashboard.'
            : 'Nenhum CPF válido encontrado no arquivo. Verifique o formato e tente novamente.');
        }
        show($('#upload-summary'));
      })
      .catch(function (err) {
        if (handleAuthError(err)) return;
        setMsg($('#upload-error'), 'Erro ao ler o arquivo: ' + (err && err.message ? err.message : 'formato não suportado.'));
      });
  }

  function preencherLista(el, itens) {
    el.textContent = '';
    itens.slice(0, 20).forEach(function (r) {
      var li = document.createElement('li');
      var cpfSpan = document.createElement('code');
      cpfSpan.textContent = r.cpf ? formatCPF(r.cpf) : '(vazio)';
      li.appendChild(cpfSpan);
      li.appendChild(document.createTextNode(' — ' + r.motivo));
      el.appendChild(li);
    });
    if (itens.length > 20) {
      var li = document.createElement('li');
      li.className = 'muted';
      li.textContent = '… e mais ' + (itens.length - 20) + '.';
      el.appendChild(li);
    }
  }

  function readFileAsRows(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onerror = function () { reject(new Error('falha na leitura do arquivo.')); };
      reader.onload = function (e) {
        try {
          var data = new Uint8Array(e.target.result);
          var wb = XLSX.read(data, { type: 'array' });
          var ws = wb.Sheets[wb.SheetNames[0]];
          if (!ws) { resolve([]); return; }
          resolve(XLSX.utils.sheet_to_json(ws, { header: 1, raw: false, defval: '' }));
        } catch (err) {
          reject(err);
        }
      };
      reader.readAsArrayBuffer(file);
    });
  }

  function normalizeCelular(raw) {
    // remove tudo que não é dígito; devolve string vazia se inválido
    var s = String(raw == null ? '' : raw).replace(/\D/g, '');
    return s.length >= 8 && s.length <= 13 ? s : '';
  }

  function formatCelular(raw) {
    if (!raw) return '';
    var d = String(raw).replace(/\D/g, '');
    if (d.length === 11) return d.replace(/(\d{2})(\d{5})(\d{4})/, '($1) $2-$3');
    if (d.length === 10) return d.replace(/(\d{2})(\d{4})(\d{4})/, '($1) $2-$3');
    return d; // formato desconhecido — devolve dígitos crus
  }

  function parseRows(rows) {
    var records = [];
    var rejected = [];
    var seen = {};
    var cpfCol = 0;
    var nomeCol = -1;
    var celCol = -1;
    var startRow = 0;

    // detecta cabeçalho procurando nome de coluna de CPF
    if (rows.length) {
      var first = rows[0] || [];
      for (var c = 0; c < first.length; c++) {
        var h = normalizeCPF(first[c]).toLowerCase();
        var raw = String(first[c] == null ? '' : first[c]).trim().toLowerCase();
        if (raw === 'cpf' || raw === 'cpf_eleitor' || h === 'cpf') {
          cpfCol = c;
          startRow = 1;
        }
        if (raw === 'nome' || raw === 'nome_eleitor' || raw === 'nome completo') {
          nomeCol = c;
          startRow = 1;
        }
        if (hasCelular() && (raw === 'celular' || raw === 'cel' || raw === 'fone' || raw === 'telefone' || raw === 'phone')) {
          celCol = c;
          startRow = 1;
        }
      }
    }

    for (var r = startRow; r < rows.length; r++) {
      var row = rows[r] || [];
      var cpfRaw = row[cpfCol];
      var cpf = normalizeCPF(cpfRaw);
      var nome = nomeCol >= 0 ? String(row[nomeCol] == null ? '' : row[nomeCol]).trim() : '';
      var celular = celCol >= 0 ? normalizeCelular(row[celCol]) : '';

      if (!cpf) {
        if (nome || row.length > 1) rejected.push({ cpf: '', motivo: 'CPF ausente' });
        continue;
      }
      if (cpf.length !== 11) {
        rejected.push({ cpf: cpf, motivo: 'CPF deve ter 11 dígitos (encontrado: ' + cpf.length + ')' });
        continue;
      }
      if (!validateCPF(cpf)) {
        rejected.push({ cpf: cpf, motivo: 'CPF inválido (dígitos verificadores não conferem)' });
        continue;
      }
      if (seen[cpf]) {
        rejected.push({ cpf: cpf, motivo: 'CPF duplicado no arquivo' });
        continue;
      }
      seen[cpf] = true;
      records.push({ cpf: cpf, nome: nome, celular: celular });
    }

    if (records.length > MAX_CPFS) {
      var extra = records.splice(MAX_CPFS);
      extra.forEach(function (rec) {
        rejected.push({ cpf: rec.cpf, motivo: 'Limite de ' + MAX_CPFS + ' CPFs por lote excedido' });
      });
    }

    return { records: records, rejected: rejected };
  }

  function confirmUpload() {
    var pending = state.pendingFile;
    if (!pending || !pending.records.length) return;

    var btn = $('#btn-confirm-upload');
    btn.disabled = true;
    hide($('#upload-summary'));
    show($('#upload-progress'));
    var fill = $('#upload-progress-fill');
    fill.style.width = '0%';
    $('#upload-progress-label').textContent = 'Criando lote…';

    var batchId = null;

    state.client
      .from('batches')
      .insert({ filename: pending.filename, total: pending.records.length, status: 'processing', user_id: state.session.user.id })
      .select('id')
      .single()
      .then(function (res) {
        if (res.error) throw res.error;
        batchId = res.data.id;

        var chain = Promise.resolve();
        var totalChunks = Math.ceil(pending.records.length / CHUNK_SIZE);
        for (var i = 0; i < totalChunks; i++) {
          (function (idx) {
            chain = chain.then(function () {
              var slice = pending.records.slice(idx * CHUNK_SIZE, (idx + 1) * CHUNK_SIZE);
              var payload = slice.map(function (rec) {
                var payloadItem = { batch_id: batchId, cpf: rec.cpf, nome: rec.nome };
                if (hasCelular() && rec.celular) payloadItem.celular = rec.celular;
                return payloadItem;
              });
              return state.client.from('voter_records').insert(payload).then(function (r) {
                if (r.error) throw r.error;
                var done = Math.min((idx + 1) * CHUNK_SIZE, pending.records.length);
                fill.style.width = Math.round((done / pending.records.length) * 100) + '%';
                $('#upload-progress-label').textContent = 'Enviando registros… ' + done + ' de ' + pending.records.length;
              });
            });
          })(i);
        }
        return chain;
      })
      .then(function () {
        resetUploadView();
        showView('dashboard');
        state.page = 0;
        return loadBatches(batchId);
      })
      .catch(function (err) {
        // compensação: remove o lote se a inserção dos registros falhou
        var cleanup = batchId
          ? state.client.from('batches').delete().eq('id', batchId)
          : Promise.resolve();
        cleanup.finally(function () {
          hide($('#upload-progress'));
          setMsg($('#upload-error'), 'Falha ao enviar o lote: ' + (err && err.message ? err.message : 'erro inesperado.') + ' O lote foi descartado.');
          show($('#upload-summary'));
          btn.disabled = false;
        });
      });
  }

  // ---------- CONSULTA AVULSA (1 CPF) ----------
  function bindAvulsa() {
    var input = $('#avulsa-cpf');
    input.addEventListener('input', function (ev) {
      var digits = normalizeCPF(ev.target.value).slice(0, 11);
      var formatted = digits;
      if (digits.length > 9) formatted = digits.replace(/(\d{3})(\d{3})(\d{3})(\d{0,2})/, '$1.$2.$3-$4');
      else if (digits.length > 6) formatted = digits.replace(/(\d{3})(\d{3})(\d{0,3})/, '$1.$2.$3');
      else if (digits.length > 3) formatted = digits.replace(/(\d{3})(\d{0,3})/, '$1.$2');
      ev.target.value = formatted;
    });
    $('#form-avulsa').addEventListener('submit', function (ev) {
      ev.preventDefault();
      submitAvulsa();
    });
    if (hasCelular()) {
      var celInput = $('#avulsa-cel');
      if (celInput) {
        celInput.addEventListener('input', function (ev) {
          var d = String(ev.target.value || '').replace(/\D/g, '').slice(0, 11);
          var f = d;
          if (d.length === 11) f = d.replace(/(\d{2})(\d{5})(\d{4})/, '($1) $2-$3');
          else if (d.length === 10) f = d.replace(/(\d{2})(\d{4})(\d{4})/, '($1) $2-$3');
          else if (d.length > 6) f = d.replace(/(\d{2})(\d{4,5})(\d{0,4})/, '($1) $2-$3');
          else if (d.length > 2) f = d.replace(/(\d{2})(\d{0,5})/, '($1) $2');
          ev.target.value = f;
        });
      }
    }
  }

  function setAvulsaMsg(text, cls) {
    var el = $('#avulsa-msg');
    el.className = 'msg avulsa-msg' + (cls ? ' ' + cls : '');
    el.textContent = text || '';
  }

  // CPF já consultado: mostra o resultado que já está salvo em vez de gastar uma
  // consulta nova, e leva o cliente até a linha correspondente.
  function mostrarJaConsultado(cpf, reg, lotes) {
    stopAvulsaWatch();
    $('#avulsa-cpf').value = '';
    if (hasCelular() && $('#avulsa-cel')) $('#avulsa-cel').value = '';

    if (!reg) {   // sumiu entre uma consulta e outra — não trava o cliente
      setAvulsaMsg('Este CPF já consta na sua base. Procure por ele no dashboard.', 'warn');
      return;
    }

    var lote = (lotes || []).find(function (b) { return b.id === reg.batch_id; });
    var onde = lote ? ' (lote ' + batchLabel(lote) + ')' : '';

    if (reg.status !== 'done') {
      setAvulsaMsg('⏳ O CPF ' + formatCPF(cpf) + ' já está na fila de consulta' + onde
        + '. O resultado aparece na tabela assim que sair — sem gastar uma nova consulta.', 'pending');
    } else {
      var extras = [];
      if (reg.zona_eleitoral) extras.push('Zona ' + reg.zona_eleitoral);
      if (reg.secao_eleitoral) extras.push('Seção ' + reg.secao_eleitoral);
      if (reg.municipio_votacao) extras.push(reg.municipio_votacao);
      var extrasTxt = extras.length ? ' — ' + extras.join(' · ') : '';
      setAvulsaMsg('✅ Este CPF já foi consultado em ' + formatDateTime(reg.checked_at) + onde
        + '. Resultado: ' + elegLabel(reg.elegibilidade) + extrasTxt
        + '. Nenhuma consulta nova foi gasta.', 'ok');
    }

    if (reg.batch_id) loadBatches(reg.batch_id);
  }

  function rememberAvulsaBatch() {
    var found = (state.batches || []).find(isAvulsaBatch);
    state.avulsaBatchId = found ? found.id : null;
  }

  function ensureAvulsaBatch() {
    if (state.avulsaBatchId) return Promise.resolve(state.avulsaBatchId);
    // procura no que já foi carregado
    var existing = (state.batches || []).find(isAvulsaBatch);
    if (existing) { state.avulsaBatchId = existing.id; return Promise.resolve(existing.id); }
    // consulta o banco (pode ter sido criado em outra aba)
    return state.client
      .from('batches')
      .select('id')
      .eq('filename', AVULSA_FILENAME)
      .eq('user_id', state.session.user.id)
      .limit(1)
      .then(function (res) {
        if (res.error) throw res.error;
        if (res.data && res.data.length) {
          state.avulsaBatchId = res.data[0].id;
          return state.avulsaBatchId;
        }
        // cria pela primeira vez
        return state.client
          .from('batches')
          .insert({ filename: AVULSA_FILENAME, total: 0, status: 'processing', user_id: state.session.user.id })
          .select('id')
          .single()
          .then(function (r) {
            if (r.error) throw r.error;
            state.avulsaBatchId = r.data.id;
            return state.avulsaBatchId;
          });
      });
  }

  function submitAvulsa() {
    var raw = $('#avulsa-cpf').value;
    var cpf = normalizeCPF(raw);
    if (cpf.length !== 11) {
      setAvulsaMsg('CPF precisa ter 11 dígitos.', 'error');
      return;
    }
    if (!validateCPF(cpf)) {
      setAvulsaMsg('CPF inválido (dígitos verificadores não conferem).', 'error');
      return;
    }
    var celular = '';
    if (hasCelular()) {
      var celInput = $('#avulsa-cel');
      if (celInput) celular = normalizeCelular(celInput.value);
    }
    var btn = $('#btn-avulsa');
    btn.disabled = true;
    setAvulsaMsg('⏳ Verificando…', 'pending');

    // Antes de gastar: este CPF já foi consultado? Era aqui que a repetição
    // acontecia — CPF digitado duas vezes com um minuto de diferença queimava
    // duas chamadas de API paga e duas consultas da cota.
    buscarJaConsultados([cpf])
      .then(function (achados) {
        var reg = achados[cpf];
        if (jaFoiConsultado(reg)) {
          return lotesParaRotulo().then(function (lotes) {
            mostrarJaConsultado(cpf, reg, lotes);
            return null;
          });
        }
        setAvulsaMsg('⏳ Enviando CPF para a fila…', 'pending');
        return ensureAvulsaBatch().then(function (batchId) {
          var payload = { batch_id: batchId, cpf: cpf };
          if (hasCelular() && celular) payload.celular = celular;
          return state.client
            .from('voter_records')
            .insert(payload)
            .select('id');
        });
      })
      .then(function (res) {
        if (res === null) return;           // já consultado — mensagem já exibida
        if (res.error) throw res.error;
        // Zero linhas = o trigger do banco descartou (corrida: o CPF entrou por
        // outra aba entre o pré-check e o insert). Mostra o que já existe.
        if (!res.data || !res.data.length) {
          return buscarJaConsultados([cpf]).then(function (achados) {
            return lotesParaRotulo().then(function (lotes) {
              mostrarJaConsultado(cpf, achados[cpf], lotes);
            });
          });
        }
        var recordId = res.data[0].id;
        setAvulsaMsg('⏳ Consultando CPF ' + formatCPF(cpf) + ' no TSE… O resultado aparece na tabela abaixo em segundos a alguns minutos.', 'pending');
        $('#avulsa-cpf').value = '';
        if (hasCelular() && $('#avulsa-cel')) $('#avulsa-cel').value = '';
        // recarrega lotes e já joga o cliente pro lote avulso
        loadBatches(state.avulsaBatchId).then(function () {
          startAvulsaWatch(recordId, cpf);
        });
      })
      .catch(function (err) {
        if (!handleAuthError(err)) {
          setAvulsaMsg('Falha ao enviar consulta: ' + (err && err.message ? err.message : 'erro inesperado.'), 'error');
        }
      })
      .finally(function () {
        btn.disabled = false;
      });
  }

  function startAvulsaWatch(recordId, cpf) {
    stopAvulsaWatch();
    var deadline = Date.now() + 20 * 60 * 1000; // desiste do polling ativo após 20 min (mas o worker segue processando)
    var timer = setInterval(function () { checkAvulsa(); }, AVULSA_POLL_MS);
    state.avulsaWatch = { recordId: recordId, cpf: cpf, timer: timer, deadline: deadline };
    // primeira checagem em 2s (às vezes o worker responde rapidão)
    setTimeout(checkAvulsa, 2000);
  }

  function stopAvulsaWatch() {
    if (state.avulsaWatch && state.avulsaWatch.timer) {
      clearInterval(state.avulsaWatch.timer);
    }
    state.avulsaWatch = null;
  }

  function checkAvulsa() {
    var w = state.avulsaWatch;
    if (!w) return;
    if (Date.now() > w.deadline) {
      setAvulsaMsg('⌛ A consulta ainda está na fila. Atualize esta página mais tarde para ver o resultado — ele fica salvo no lote "Consultas avulsas".', 'warn');
      stopAvulsaWatch();
      return;
    }
    state.client
      .from('voter_records')
      .select('id, cpf, status, attempts, elegibilidade, nome, zona_eleitoral, secao_eleitoral, municipio_votacao, checked_at')
      .eq('id', w.recordId)
      .single()
      .then(function (res) {
        if (res.error) return; // silencioso — tenta de novo
        var r = res.data;
        if (r.status === 'done') {
          stopAvulsaWatch();
          var label = elegLabel(r.elegibilidade);
          var extras = [];
          if (r.zona_eleitoral) extras.push('Zona ' + r.zona_eleitoral);
          if (r.secao_eleitoral) extras.push('Seção ' + r.secao_eleitoral);
          if (r.municipio_votacao) extras.push(r.municipio_votacao);
          var extrasTxt = extras.length ? ' — ' + extras.join(' · ') : '';
          setAvulsaMsg('✅ Resultado: ' + label + extrasTxt + '. Consulta salva na tabela abaixo.', 'ok');
          // recarrega a página do lote para o cliente ver na tabela
          if (state.currentBatch && state.currentBatch.id === state.avulsaBatchId) {
            loadRecordsPage();
            loadElegCards();
            loadStatusCards();
          }
        } else if (r.status === 'error' && (r.attempts || 0) >= 5) {
          stopAvulsaWatch();
          setAvulsaMsg('❌ Não foi possível concluir a consulta agora. Tente novamente em alguns minutos.', 'error');
        }
        // demais status: sigo esperando
      });
  }

  // ---------- DASHBOARD ----------
  function bindDashboard() {
    $('#btn-new-upload').addEventListener('click', function () {
      stopAutoRefresh();
      showView('upload');
    });
    $('#btn-refresh').addEventListener('click', function () {
      refreshDashboard();
    });
    $('#btn-export').addEventListener('click', exportCSV);
    $('#batch-select').addEventListener('change', function (ev) {
      state.page = 0;
      $('#filter-search').value = '';
      selectBatch(ev.target.value);
    });
    $('#filter-elegibilidade').addEventListener('change', function () {
      state.page = 0;
      loadRecordsPage();
    });
    $('#filter-search').addEventListener('input', debounce(function () {
      state.page = 0;
      loadRecordsPage();
    }, 300));
    $('#btn-prev-page').addEventListener('click', function () {
      if (state.page > 0) { state.page--; loadRecordsPage(); }
    });
    $('#btn-next-page').addEventListener('click', function () {
      state.page++;
      loadRecordsPage();
    });
  }

  function resetDashboardView() {
    stopAutoRefresh();
    state.batches = [];
    state.currentBatch = null;
    $('#batch-select').textContent = '';
    $('#batch-badge').textContent = '';
    $('#batch-badge').className = 'badge';
    hide($('#batch-info-card'));
    $('#status-cards').textContent = '';
    $('#eleg-cards').textContent = '';
    $('#records-tbody').textContent = '';
    $('#page-info').textContent = '';
    setMsg($('#dashboard-error'), '');
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      clearTimeout(t);
      var args = arguments, self = this;
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  function loadBatches(selectId) {
    return state.client
      .from('batches')
      .select('id, filename, total, status, created_at')
      .order('created_at', { ascending: false })
      .then(function (res) {
        if (res.error) {
          if (handleAuthError(res.error)) return;
          setMsg($('#dashboard-error'), 'Erro ao carregar lotes: ' + res.error.message);
          return;
        }
        state.batches = res.data || [];
        rememberAvulsaBatch();
        var sel = $('#batch-select');
        sel.textContent = '';
        state.batches.forEach(function (b) {
          var opt = document.createElement('option');
          opt.value = b.id;
          opt.textContent = batchLabel(b);
          sel.appendChild(opt);
        });
        if (!state.batches.length) {
          var opt = document.createElement('option');
          opt.value = '';
          opt.textContent = 'Nenhum lote enviado ainda';
          sel.appendChild(opt);
          resetBatchDetail();
          return;
        }
        var target = selectId || (state.currentBatch && state.currentBatch.id) || state.batches[0].id;
        sel.value = target;
        selectBatch(target);
      });
  }

  function resetBatchDetail() {
    stopAutoRefresh();
    state.currentBatch = null;
    $('#batch-badge').textContent = '';
    $('#batch-badge').className = 'badge';
    hide($('#batch-info-card'));
    $('#status-cards').textContent = '';
    $('#eleg-cards').textContent = '';
    $('#records-tbody').textContent = '';
    $('#page-info').textContent = '';
  }

  function selectBatch(id) {
    var batch = state.batches.find(function (b) { return b.id === id; });
    state.currentBatch = batch || null;
    if (!batch) { resetBatchDetail(); return; }
    renderBatchDetail();
    loadStatusCards();
    loadElegCards();
    loadRecordsPage();
    manageAutoRefresh();
  }

  function refreshDashboard() {
    if (!state.currentBatch) return;
    var currentId = state.currentBatch.id;
    var currentPage = state.page;
    state.client
      .from('batches')
      .select('id, filename, total, status, created_at')
      .order('created_at', { ascending: false })
      .then(function (res) {
        if (res.error) {
          if (handleAuthError(res.error)) return;
          return;
        }
        state.batches = res.data || [];
        rememberAvulsaBatch();
        var sel = $('#batch-select');
        var prevValue = sel.value;
        sel.textContent = '';
        state.batches.forEach(function (b) {
          var opt = document.createElement('option');
          opt.value = b.id;
          opt.textContent = batchLabel(b);
          sel.appendChild(opt);
        });
        var keep = state.batches.some(function (b) { return b.id === currentId; }) ? currentId : prevValue;
        if (keep) sel.value = keep;
        selectBatch(keep || (state.batches[0] && state.batches[0].id));
        state.page = currentPage;
        loadRecordsPage();
      });
  }

  function renderBatchDetail() {
    var b = state.currentBatch;
    if (!b) return;

    var avulsa = isAvulsaBatch(b);
    var badge = $('#batch-badge');
    if (avulsa) {
      badge.textContent = 'Consultas avulsas';
      badge.className = 'badge';
    } else {
      badge.textContent = statusLabel(b.status);
      badge.className = 'badge badge-' + b.status;
    }

    $('#batch-meta').textContent = '';
    var meta = avulsa
      ? [['Lote', 'Consultas avulsas'], ['Criado em', formatDateTime(b.created_at)]]
      : [['Arquivo', b.filename], ['Total de CPFs', String(b.total)], ['Enviado em', formatDateTime(b.created_at)]];
    meta.forEach(function (pair) {
      var div = document.createElement('div');
      div.className = 'batch-meta-item';
      var k = document.createElement('span');
      k.className = 'batch-meta-key';
      k.textContent = pair[0];
      var v = document.createElement('span');
      v.className = 'batch-meta-value';
      v.textContent = pair[1];
      div.appendChild(k);
      div.appendChild(v);
      $('#batch-meta').appendChild(div);
    });
    // barra de progresso não faz sentido pra avulsas (sem total fixo)
    if (avulsa) {
      $('#batch-progress-fill').style.width = '0%';
      $('#batch-progress-label').textContent = '';
      $('#batch-progress-fill').parentElement.style.display = 'none';
    } else {
      $('#batch-progress-fill').parentElement.style.display = '';
    }
    show($('#batch-info-card'));
  }

  function statusLabel(s) {
    var map = { processing: 'Processando', done: 'Concluído', error: 'Erro' };
    return map[s] || s || '—';
  }

  function statusRecordLabel(s) {
    var map = {
      pending: 'Aguardando', enriching: 'Enriquecendo', ready_tse: 'Pronto p/ TSE',
      checking: 'Consultando TSE', done: 'Concluído', error: 'Erro',
    };
    return map[s] || s || '—';
  }

  function elegLabel(s) {
    var map = {
      apto: 'Apto', inapto_cancelado: 'Inapto — cancelado', inapto_suspenso: 'Inapto — suspenso',
      inapto_transferido: 'Inapto — transferido', regularizar_tse: 'Regularizar no TSE',
    };
    return map[s] || s || '—';
  }

  function loadStatusCards() {
    var b = state.currentBatch;
    if (!b) return;
    var statuses = ['pending', 'enriching', 'ready_tse', 'checking', 'done', 'error'];
    Promise.all(statuses.map(function (s) {
      return state.client
        .from('voter_records')
        .select('id', { count: 'exact', head: true })
        .eq('batch_id', b.id)
        .eq('status', s);
    })).then(function (results) {
      if (results.some(function (r) { return r.error; })) return; // cards secundários
      var wrap = $('#status-cards');
      wrap.textContent = '';
      var doneCount = 0, errorCount = 0, total = 0;
      results.forEach(function (res, i) {
        var n = res.count == null ? 0 : res.count;
        total += n;
        if (statuses[i] === 'done') doneCount += n;
        if (statuses[i] === 'error') errorCount += n;
        if (n > 0) wrap.appendChild(makeCard(statusRecordLabel(statuses[i]), n, 'status-' + statuses[i]));
      });
      if (!total) wrap.appendChild(makeCard('Sem registros', 0, ''));

        // barra de progresso — só faz sentido para lote com total fixo
        if (!isAvulsaBatch(b)) {
          var progress = Math.min(1, (doneCount + errorCount) / (b.total || 1));
          $('#batch-progress-fill').style.width = Math.round(progress * 100) + '%';
          $('#batch-progress-label').textContent = doneCount + errorCount + ' de ' + b.total + ' verificados (' + Math.round(progress * 100) + '%)';
        }
      });
  }

  function loadElegCards() {
    var b = state.currentBatch;
    if (!b) return;
    var elegibilidades = ['apto', 'inapto_cancelado', 'inapto_suspenso', 'inapto_transferido', 'regularizar_tse'];
    var wrap = $('#eleg-cards');
    wrap.textContent = '';
    var queries = elegibilidades.map(function (e) {
      return state.client
        .from('voter_records')
        .select('id', { count: 'exact', head: true })
        .eq('batch_id', b.id)
        .eq('elegibilidade', e);
    });
    Promise.all(queries).then(function (results) {
      results.forEach(function (res, i) {
        if (res.error) return;
        wrap.appendChild(makeCard(elegLabel(elegibilidades[i]), res.count == null ? 0 : res.count, 'eleg'));
      });
      // elegibilidades fora da lista fixa
      state.client
        .from('voter_records')
        .select('elegibilidade')
        .eq('batch_id', b.id)
        .not('elegibilidade', 'in', '("apto","inapto_cancelado","inapto_suspenso","inapto_transferido","regularizar_tse")')
        .then(function (res2) {
          if (res2.error || !res2.data) return;
          var other = {};
          res2.data.forEach(function (row) {
            var k = row.elegibilidade || '—';
            other[k] = (other[k] || 0) + 1;
          });
          Object.keys(other).forEach(function (k) {
            wrap.appendChild(makeCard(k, other[k], 'eleg'));
          });
        });
    });
  }

  function makeCard(label, value, cls) {
    var div = document.createElement('div');
    div.className = 'stat-card ' + (cls || '');
    var v = document.createElement('span');
    v.className = 'stat-card-value';
    v.textContent = String(value);
    var l = document.createElement('span');
    l.className = 'stat-card-label';
    l.textContent = label;
    div.appendChild(v);
    div.appendChild(l);
    return div;
  }

  function loadRecordsPage() {
    var b = state.currentBatch;
    if (!b) return;
    var from = state.page * PAGE_SIZE;
    var to = from + PAGE_SIZE - 1;

    var query = state.client
      .from('voter_records')
      .select('cpf, nome, celular, elegibilidade, zona_eleitoral, secao_eleitoral, municipio_votacao, checked_at', { count: 'exact' })
      .eq('batch_id', b.id)
      .order('id', { ascending: false })
      .range(from, to);

    var eleg = $('#filter-elegibilidade').value;
    if (eleg) query = query.eq('elegibilidade', eleg);

    var search = $('#filter-search').value.trim();
    if (search) {
      var digits = normalizeCPF(search);
      if (digits.length >= 3 && digits.length <= 11 && /^\d+$/.test(digits)) {
        query = query.filter('cpf', 'like', digits + '%');
      } else {
        query = query.ilike('nome', '%' + search + '%');
      }
    }

    query.then(function (res) {
      if (res.error) {
        if (handleAuthError(res.error)) return;
        setMsg($('#dashboard-error'), 'Erro ao carregar registros: ' + res.error.message);
        return;
      }
      setMsg($('#dashboard-error'), '');
      renderRecordsTable(res.data || []);
      var total = res.count || 0;
      var totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      if (state.page >= totalPages) state.page = totalPages - 1;
      $('#page-info').textContent = 'Página ' + (state.page + 1) + ' de ' + totalPages + ' (' + total + ' registros)';
      $('#btn-prev-page').disabled = state.page === 0;
      $('#btn-next-page').disabled = state.page >= totalPages - 1;
    });
  }

  function renderRecordsTable(rows) {
    var tbody = $('#records-tbody');
    tbody.textContent = '';
    if (!rows.length) {
      var tr = document.createElement('tr');
      var td = document.createElement('td');
      td.colSpan = hasCelular() ? 8 : 7;
      td.className = 'empty-row';
      td.textContent = 'Nenhum registro encontrado.';
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    rows.forEach(function (r) {
      var tr = document.createElement('tr');

      var tdCpf = document.createElement('td');
      tdCpf.textContent = formatCPF(r.cpf || '');
      tr.appendChild(tdCpf);

      var tdNome = document.createElement('td');
      tdNome.textContent = r.nome || '—';
      tr.appendChild(tdNome);

      if (hasCelular()) {
        var tdCel = document.createElement('td');
        tdCel.textContent = formatCelular(r.celular) || '—';
        tdCel.className = 'cel';
        tr.appendChild(tdCel);
      }

      var tdEleg = document.createElement('td');
      tdEleg.textContent = elegLabel(r.elegibilidade);
      if (r.elegibilidade) tdEleg.className = 'eleg eleg-' + r.elegibilidade;
      tr.appendChild(tdEleg);

      // "TSE não retornou": aptidão foi obtida mas o TSE bloqueou o endpoint
      // 'onde-votar' (403 do próprio TSE — não é falha nossa). Só marca se
      // elegibilidade é apto/inapto_* (regularizar_tse é vazio naturalmente).
      var elegAptidaoOK = r.elegibilidade && r.elegibilidade.indexOf('regularizar') !== 0;
      var tseNaoRetornou = elegAptidaoOK && !r.zona_eleitoral;

      var tdZona = document.createElement('td');
      if (tseNaoRetornou) {
        tdZona.textContent = 'TSE não retornou';
        tdZona.className = 'tse-nao-retornou';
        tdZona.title = 'A base pública do TSE não liberou o local de votação para este CPF.';
      } else {
        tdZona.textContent = r.zona_eleitoral || '—';
      }
      tr.appendChild(tdZona);

      var tdSecao = document.createElement('td');
      tdSecao.textContent = tseNaoRetornou ? '—' : (r.secao_eleitoral || '—');
      if (tseNaoRetornou) tdSecao.className = 'tse-nao-retornou';
      tr.appendChild(tdSecao);

      var tdMun = document.createElement('td');
      tdMun.textContent = tseNaoRetornou ? '—' : (r.municipio_votacao || '—');
      if (tseNaoRetornou) tdMun.className = 'tse-nao-retornou';
      tr.appendChild(tdMun);

      var tdChecked = document.createElement('td');
      tdChecked.textContent = formatDateTime(r.checked_at);
      tr.appendChild(tdChecked);

      tbody.appendChild(tr);
    });
  }

  function manageAutoRefresh() {
    stopAutoRefresh();
    if (state.currentBatch && state.currentBatch.status === 'processing') {
      state.autoRefreshTimer = setInterval(function () {
        if (state.currentBatch && state.currentBatch.status === 'processing') {
          refreshDashboard();
        } else {
          stopAutoRefresh();
        }
      }, AUTO_REFRESH_MS);
    }
  }

  function stopAutoRefresh() {
    if (state.autoRefreshTimer) {
      clearInterval(state.autoRefreshTimer);
      state.autoRefreshTimer = null;
    }
  }

  // ---------- EXPORT CSV ----------
  function exportCSV() {
    var b = state.currentBatch;
    if (!b) return;
    if (state.exporting) return;
    state.exporting = true;
    var btn = $('#btn-export');
    btn.disabled = true;
    btn.textContent = 'Exportando…';

    var HEADER = ['cpf', 'nome'];
    if (hasCelular()) HEADER.push('celular');
    HEADER = HEADER.concat(['nome_mae', 'data_nascimento', 'elegibilidade', 'titulo_eleitoral',
      'zona_eleitoral', 'secao_eleitoral', 'municipio_votacao', 'uf', 'biometria',
      'obrigacao_eleitoral', 'motivo_situacao', 'ano_situacao', 'checked_at']);
    var all = [];
    var lastId = 0;

    function fetchNext() {
      var selectCols = 'id, cpf, nome' + (hasCelular() ? ', celular' : '') +
        ', nome_mae, data_nascimento, elegibilidade, titulo_eleitoral, zona_eleitoral, secao_eleitoral, municipio_votacao, uf, biometria, obrigacao_eleitoral, motivo_situacao, ano_situacao, checked_at';
      return state.client
        .from('voter_records')
        .select(selectCols)
        .eq('batch_id', b.id)
        .order('id', { ascending: true })
        .gt('id', lastId)
        .range(0, EXPORT_CHUNK - 1)
        .then(function (res) {
          if (res.error) throw res.error;
          var rows = res.data || [];
          if (rows.length) lastId = rows[rows.length - 1].id;
          all = all.concat(rows);
          if (rows.length === EXPORT_CHUNK) return fetchNext();
        });
    }

    fetchNext()
      .then(function () {
        var lines = [HEADER.join(';')];
        all.forEach(function (r) {
          lines.push(HEADER.map(function (col) {
            return csvCell(r[col]);
          }).join(';'));
        });
        // BOM para o Excel abrir acentos corretamente
        var blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        var safeName = (b.filename || 'lote').replace(/\.[^.]+$/, '').replace(/[^\w\-]+/g, '_');
        a.href = url;
        a.download = 'resultado_' + safeName + '.csv';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      })
      .catch(function (err) {
        if (!handleAuthError(err)) {
          setMsg($('#dashboard-error'), 'Erro ao exportar: ' + (err && err.message ? err.message : 'erro inesperado.'));
        }
      })
      .finally(function () {
        state.exporting = false;
        btn.disabled = false;
        btn.textContent = 'Exportar CSV';
      });
  }

  function csvCell(value) {
    if (value == null) return '';
    var s = String(value);
    if (/[";\n\r]/.test(s)) {
      s = '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  }

  // ---------- go ----------
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
