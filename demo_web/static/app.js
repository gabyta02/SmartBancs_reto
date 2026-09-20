"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (t) => String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const POLL_INICIAL_MS = 1000;
const POLL_INTERVALO_MS = 1000;
const POLL_MAX_MS = 15000;

let config = { grafana_url: "http://localhost:3000", jaeger_url: "http://localhost:18086" };

/* ------------------------------------------------------------ navegación */
function mostrarVista() {
  const vista = (location.hash || "#dashboard").slice(1);
  document.querySelectorAll(".vista").forEach((v) => v.classList.toggle("activa", v.id === "vista-" + vista));
  document.querySelectorAll(".sidebar a").forEach((a) => a.classList.toggle("activo", a.dataset.vista === vista));
  if (vista === "dashboard") cargarEstado();
}
window.addEventListener("hashchange", mostrarVista);

/* ------------------------------------------------------------- dashboard */
async function cargarEstado() {
  try {
    const r = await fetch("/demo/estado");
    const estado = await r.json();
    document.querySelectorAll(".estado").forEach((card) => {
      const ok = estado[card.dataset.servicio];
      card.classList.toggle("ok", ok === true);
      card.classList.toggle("mal", ok === false);
      card.querySelector("p").textContent = ok ? "Operativo" : "Sin respuesta";
    });
  } catch {
    document.querySelectorAll(".estado p").forEach((p) => (p.textContent = "Demo no disponible"));
  }
}

/* --------------------------------------------------------- transferencias */
let cuentas = [];

// Selectores buscables (input + datalist). La cuenta elegida en un lado se excluye del otro.
function pintarListas() {
  const form = $("#form-transferencia");
  const origen = form.numero_cuenta_origen.value.trim();
  const destino = form.numero_cuenta_destino.value.trim();
  const opciones = (excluir) =>
    cuentas
      .filter((c) => c.numero_cuenta !== excluir)
      .map((c) => `<option value="${esc(c.numero_cuenta)}">${esc(c.numero_cuenta)} · ${esc(c.divisa)}</option>`)
      .join("");
  $("#lista-origen").innerHTML = opciones(destino);
  $("#lista-destino").innerHTML = opciones(origen);
}

async function cargarCuentas() {
  try {
    const r = await fetch("/demo/cuentas");
    if (r.ok) cuentas = (await r.json()).cuentas;
  } catch { /* el formulario sigue funcionando escribiendo el numero */ }
  pintarListas();
}

let sondeoActual = 0; // invalida sondeos de transferencias anteriores

function filas(pares) {
  return "<dl>" + pares.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("") + "</dl>";
}

async function transferir(evento) {
  evento.preventDefault();
  const f = new FormData(evento.target);
  // La clave de idempotencia es tecnica: se genera por envio y el usuario no la ve.
  const cuerpo = {
    clave_idempotencia: crypto.randomUUID(),
    numero_cuenta_origen: f.get("numero_cuenta_origen").trim(),
    numero_cuenta_destino: f.get("numero_cuenta_destino").trim(),
    monto: f.get("monto"),
    divisa: f.get("divisa").trim().toUpperCase(),
  };

  if (cuerpo.numero_cuenta_origen === cuerpo.numero_cuenta_destino) {
    $("#resultado").hidden = false;
    $("#resultado").innerHTML = '<p class="resultado-titulo mal">La cuenta origen y destino deben ser diferentes.</p>';
    return;
  }

  const resultado = $("#resultado");
  const tarjetaIA = $("#tarjeta-ia");
  const enlaces = $("#enlaces-transferencia");
  const boton = $("#btn-transferir");
  sondeoActual++;
  tarjetaIA.hidden = true;
  enlaces.hidden = true;
  resultado.hidden = false;
  resultado.innerHTML = '<p class="suave">Procesando transferencia...</p>';
  boton.disabled = true;

  try {
    const r = await fetch("/demo/transacciones", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cuerpo),
    });
    const datos = await r.json();

    if (!r.ok) {
      const detalle = typeof datos.detail === "string" ? datos.detail : JSON.stringify(datos.detail);
      resultado.innerHTML =
        `<p class="resultado-titulo mal">No se pudo realizar la transferencia</p>` +
        filas([["Motivo", detalle]].concat(datos.tiempo_ms != null ? [["Tiempo", datos.tiempo_ms + " ms"]] : []));
      return;
    }

    const t = datos.transaccion;
    const completada = t.estado === "COMPLETADA";
    const pares = [
      ["ID", t.id_transaccion],
      ["Estado", t.estado],
    ];
    if (t.razon_fallo) pares.push(["Motivo", t.razon_fallo]);
    pares.push(["Monto", `${Number(t.monto).toFixed(2)} ${t.divisa}`], ["Tiempo", datos.tiempo_ms + " ms"]);
    resultado.innerHTML =
      `<p class="resultado-titulo ${completada ? "ok" : "mal"}">Transferencia ${completada ? "completada" : t.estado.toLowerCase()}</p>` + filas(pares);

    enlaces.hidden = false;
    $("#btn-jaeger").href = datos.jaeger_trace_url || config.jaeger_url;

    if (completada) esperarAnalisis(t.id_transaccion);
  } catch {
    resultado.innerHTML = '<p class="resultado-titulo mal">No se pudo contactar con la demo</p>';
  } finally {
    boton.disabled = false;
  }
}

async function esperarAnalisis(idTransaccion) {
  const miSondeo = sondeoActual;
  const tarjeta = $("#tarjeta-ia");
  const contenido = $("#contenido-ia");
  tarjeta.hidden = false;
  contenido.innerHTML = '<p class="suave">Generando recomendación IA...</p>';

  const inicio = Date.now();
  let espera = POLL_INICIAL_MS;
  while (Date.now() - inicio < POLL_MAX_MS) {
    await new Promise((res) => setTimeout(res, espera));
    if (miSondeo !== sondeoActual) return; // el usuario lanzó otra transferencia
    espera = POLL_INTERVALO_MS;
    try {
      const r = await fetch("/demo/analisis/" + idTransaccion);
      if (r.ok) {
        const a = await r.json();
        if (a.estado === "DISPONIBLE") {
          contenido.innerHTML = filas([
            ["Nivel", a.nivel],
            ["Score", Number(a.score).toFixed(2)],
            ["Modelo", a.modelo],
            ["Recomendación", a.recomendacion],
          ]);
          return;
        }
        contenido.innerHTML = '<p class="suave">Procesando recomendación...</p>';
      }
    } catch { /* se reintenta en el siguiente ciclo */ }
  }
  if (miSondeo === sondeoActual) {
    contenido.innerHTML = '<p class="aviso">El análisis todavía está siendo procesado.</p>';
  }
}

/* ------------------------------------------------------------------- ETL */
let archivoEtl = null;

function elegirArchivo(archivo) {
  archivoEtl = archivo || null;
  $("#archivo-elegido").textContent = archivoEtl ? "Archivo seleccionado: " + archivoEtl.name : "Ningún archivo seleccionado";
  $("#btn-etl").disabled = !archivoEtl;
}

function iniciarZonaArchivo() {
  const zona = $("#zona-archivo");
  const input = $("#archivo-etl");
  input.addEventListener("change", () => elegirArchivo(input.files[0]));
  ["dragenter", "dragover"].forEach((e) =>
    zona.addEventListener(e, (ev) => { ev.preventDefault(); zona.classList.add("arrastrando"); }));
  ["dragleave", "drop"].forEach((e) =>
    zona.addEventListener(e, (ev) => { ev.preventDefault(); zona.classList.remove("arrastrando"); }));
  zona.addEventListener("drop", (ev) => elegirArchivo(ev.dataTransfer.files[0]));
}

async function ejecutarEtl() {
  if (!archivoEtl) return;
  const boton = $("#btn-etl");
  const salida = $("#resultado-etl");
  boton.disabled = true; // evita envios duplicados mientras procesa
  salida.hidden = false;
  salida.innerHTML = '<p class="suave">Procesando archivo...</p>';
  try {
    const datos = new FormData();
    datos.append("archivo", archivoEtl);
    const r = await fetch("/demo/etl/ejecutar", { method: "POST", body: datos });
    const d = await r.json();
    if (d.estado === "COMPLETADO") {
      salida.innerHTML = filas([
        ["Estado", d.estado],
        ["Archivo", d.archivo],
        ["Registros leídos", d.registros_leidos],
        ["Registros transformados", d.registros_transformados],
        ["Registros cargados", d.registros_cargados],
        ["Registros rechazados", d.registros_rechazados],
        ["Duración", d.duracion_ms + " ms"],
      ]);
    } else {
      const pares = [["Estado", d.estado || "FALLIDO"], ["Detalle", d.detalle || "No se pudo ejecutar el ETL."]];
      if (d.archivo) pares.splice(1, 0, ["Archivo", d.archivo]);
      salida.innerHTML = filas(pares);
    }
  } catch {
    salida.innerHTML = '<p class="aviso">No se pudo contactar con la demo.</p>';
  } finally {
    boton.disabled = !archivoEtl;
  }
}

/* ------------------------------------------------------------------ init */
function aplicarEnlaces() {
  document.querySelectorAll('[data-enlace="grafana"]').forEach((a) => (a.href = config.grafana_url));
  document.querySelectorAll('[data-enlace="jaeger"]').forEach((a) => (a.href = config.jaeger_url));
}

async function iniciar() {
  try {
    config = await (await fetch("/demo/config")).json();
    const form = $("#form-transferencia");
    if (config.cuenta_origen) form.numero_cuenta_origen.value = config.cuenta_origen;
    if (config.cuenta_destino) form.numero_cuenta_destino.value = config.cuenta_destino;
  } catch { /* se usan los valores por defecto */ }
  aplicarEnlaces();
  $("#form-transferencia").addEventListener("submit", transferir);
  $("#form-transferencia").addEventListener("input", pintarListas);
  cargarCuentas();
  $("#btn-etl").addEventListener("click", ejecutarEtl);
  iniciarZonaArchivo();
  mostrarVista();
}

iniciar();
