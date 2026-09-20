import http from "k6/http";
import { check } from "k6";
import { Counter, Rate } from "k6/metrics";
import { SharedArray } from "k6/data";
import exec from "k6/execution";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const RATE = Number(__ENV.RATE || 100);
const DURATION = __ENV.DURATION || "30s";
const DATASET = (__ENV.DATASET || "HOT").toUpperCase();

const PRE_VUS = Number(__ENV.PRE_VUS || 100);
const MAX_VUS = Number(__ENV.MAX_VUS || 2000);

const DEBUG = (__ENV.DEBUG || "false").toLowerCase() === "true";

// -----------------------------------------------------------------------------
// Métricas
// -----------------------------------------------------------------------------

const transaccionesCompletadas = new Counter(
    "transacciones_completadas"
);

const transaccionesRechazadas = new Counter(
    "transacciones_rechazadas"
);

const erroresTecnicos = new Counter(
    "errores_tecnicos"
);

const tasaTransaccionesCompletadas = new Rate(
    "tasa_transacciones_completadas"
);

// -----------------------------------------------------------------------------
// Dataset HOT
// -----------------------------------------------------------------------------

const CUENTAS_HOT = [
    "777355db-0986-43ef-b1f4-c7ee9f290b56",
    "d251cdcc-d5f3-4383-8c48-43f28a465503",
    "09f32cc9-a6b5-4bcc-bf3f-fc713b01ebec",
    "51053c4b-e3f5-4eab-9ff7-43313ec04a5a",
    "f4b97f89-b13f-4bc6-b96b-ab1f3a9b9a12",
    "5923c7bf-beae-4ee7-b863-debe0f2d8840",
    "eb69c365-875a-44d2-b621-fb0279d89f03",
    "569616d3-a399-4d71-9c17-c441d93da261",
    "f73d1421-f0a9-404c-b95b-2b34888cf6ad",
    "a1011e73-6203-43ff-8ce9-94b6ba25180a",
    "32fbcd13-6d86-482d-96b1-0668517cf960",
    "aea61803-b243-45a9-817f-03c5b840b6d4",
    "70fa510c-0fec-4b57-8471-676c8d35561f",
    "42fdc87c-29b3-4845-9e2c-830d73d70592",
    "e1e173f4-7332-4b88-b5cc-8181b05a6dbc",
    "31fb3375-fbb3-440a-ac93-6208932b7468",
    "70e89d0c-eae9-4c64-b35f-6f9441e0ce62",
    "78827aee-00e5-4cd3-b2e6-51f015efda31",
    "9eb3ae66-328c-4570-8c31-9f367b3fab61",
    "d18cc3a9-7d68-4e97-af72-6a7799a13614",
    "cd4fe7cf-5881-4cb2-90ba-57b3a3f0ad1b",
    "83a943e9-ebd5-4f16-8ecb-320354264ff6",
    "a7f52dd2-9c89-46fc-8e93-05bef213927a",
];

const CUENTAS_HOT_ORIGEN = [
    "777355db-0986-43ef-b1f4-c7ee9f290b56",
    "d251cdcc-d5f3-4383-8c48-43f28a465503",
    "09f32cc9-a6b5-4bcc-bf3f-fc713b01ebec",
    "51053c4b-e3f5-4eab-9ff7-43313ec04a5a",
    "f4b97f89-b13f-4bc6-b96b-ab1f3a9b9a12",
    "5923c7bf-beae-4ee7-b863-debe0f2d8840",
    "eb69c365-875a-44d2-b621-fb0279d89f03",
    "f73d1421-f0a9-404c-b95b-2b34888cf6ad",
    "a1011e73-6203-43ff-8ce9-94b6ba25180a",
    "aea61803-b243-45a9-817f-03c5b840b6d4",
    "70fa510c-0fec-4b57-8471-676c8d35561f",
    "42fdc87c-29b3-4845-9e2c-830d73d70592",
    "e1e173f4-7332-4b88-b5cc-8181b05a6dbc",
    "31fb3375-fbb3-440a-ac93-6208932b7468",
    "70e89d0c-eae9-4c64-b35f-6f9441e0ce62",
    "78827aee-00e5-4cd3-b2e6-51f015efda31",
    "9eb3ae66-328c-4570-8c31-9f367b3fab61",
    "d18cc3a9-7d68-4e97-af72-6a7799a13614",
    "cd4fe7cf-5881-4cb2-90ba-57b3a3f0ad1b",
    "83a943e9-ebd5-4f16-8ecb-320354264ff6",
    "a7f52dd2-9c89-46fc-8e93-05bef213927a",
];

// -----------------------------------------------------------------------------
// Dataset LOAD
// -----------------------------------------------------------------------------

const CUENTAS_LOAD = new SharedArray(
    "cuentas_load",
    function () {
        return JSON.parse(open("./cuentas_load.json"));
    }
);

// -----------------------------------------------------------------------------
// Selección de dataset
// -----------------------------------------------------------------------------

let cuentasOrigen;
let cuentasDestino;

if (DATASET === "LOAD") {
    cuentasOrigen = CUENTAS_LOAD;
    cuentasDestino = CUENTAS_LOAD;
} else if (DATASET === "HOT") {
    cuentasOrigen = CUENTAS_HOT_ORIGEN;
    cuentasDestino = CUENTAS_HOT;
} else {
    throw new Error(
        `DATASET inválido: ${DATASET}. Usa HOT o LOAD.`
    );
}

if (cuentasOrigen.length < 2 || cuentasDestino.length < 2) {
    throw new Error(
        `Dataset ${DATASET} no tiene suficientes cuentas`
    );
}

// -----------------------------------------------------------------------------
// Configuración k6
// -----------------------------------------------------------------------------

export const options = {
    scenarios: {
        transacciones: {
            executor: "constant-arrival-rate",
            rate: RATE,
            timeUnit: "1s",
            duration: DURATION,
            preAllocatedVUs: PRE_VUS,
            maxVUs: MAX_VUS,
        },
    },

    thresholds: {
        http_req_failed: [
            "rate<0.01",
        ],

        http_req_duration: [
            "p(95)<2000",
        ],

        // Solo informativos: fuerzan a k6 a mostrar el conteo por status.
        "http_reqs{status:201}": ["count>=0"],
        "http_reqs{status:503}": ["count>=0"],
        "http_reqs{status:500}": ["count>=0"],
        "http_reqs{status:409}": ["count>=0"],
        "http_reqs{status:404}": ["count>=0"],
        "http_reqs{status:422}": ["count>=0"],
        "http_reqs{status:0}": ["count>=0"],
    },
};

// -----------------------------------------------------------------------------
// Setup
// -----------------------------------------------------------------------------

export function setup() {
    console.log(
        `Dataset=${DATASET} | origenes=${cuentasOrigen.length} | destinos=${cuentasDestino.length} | RATE=${RATE} | DURATION=${DURATION}`
    );
}

// -----------------------------------------------------------------------------
// Idempotencia
// -----------------------------------------------------------------------------

function generarClaveIdempotencia(iteracion) {
    return [
        "k6",
        DATASET.toLowerCase(),
        Date.now(),
        __VU,
        iteracion,
    ].join("-");
}

// -----------------------------------------------------------------------------
// Selección determinista de cuentas
// -----------------------------------------------------------------------------

function seleccionarCuentas(iteracion) {
    const totalOrigen = cuentasOrigen.length;
    const totalDestino = cuentasDestino.length;

    const indiceOrigen =
        iteracion % totalOrigen;

    let indiceDestino =
        (iteracion * 7 + 3) % totalDestino;

    const origen =
        cuentasOrigen[indiceOrigen];

    let destino =
        cuentasDestino[indiceDestino];

    if (origen === destino) {
        indiceDestino =
            (indiceDestino + 1) % totalDestino;

        destino =
            cuentasDestino[indiceDestino];
    }

    return {
        origen,
        destino,
    };
}

// -----------------------------------------------------------------------------
// Test
// -----------------------------------------------------------------------------

export default function () {
    const iteracion =
        exec.scenario.iterationInTest;

    const {
        origen,
        destino,
    } = seleccionarCuentas(iteracion);

    const payload = JSON.stringify({
        id_idempotencia:
            generarClaveIdempotencia(iteracion),

        cuenta_origen_id:
            origen,

        cuenta_destino_id:
            destino,

        monto: 0.01,

        divisa: "USD",
    });

    const params = {
        headers: {
            "Content-Type": "application/json",
        },

        timeout: "10s",
    };

    const respuesta = http.post(
        `${BASE_URL}/transacciones`,
        payload,
        params
    );

    let cuerpo = null;

    try {
        cuerpo = respuesta.json();
    } catch (_) {
        cuerpo = null;
    }

    const es201 =
        respuesta.status === 201;

    const completada =
        es201 &&
        cuerpo &&
        cuerpo.estado === "COMPLETADA";

    const rechazada =
        es201 &&
        cuerpo &&
        cuerpo.estado === "RECHAZADA";

    const errorTecnico =
        !es201;

    const menorDosSegundos =
        respuesta.timings.duration < 2000;

    check(respuesta, {
        "HTTP 201": () =>
            es201,

        "transferencia completada o rechazada": () =>
            completada || rechazada,

        "respuesta menor a 2 segundos": () =>
            menorDosSegundos,
    });

    tasaTransaccionesCompletadas.add(
        completada
    );

    if (completada) {
        transaccionesCompletadas.add(1);
    }

    if (rechazada) {
        transaccionesRechazadas.add(1);
    }

    if (errorTecnico) {
        erroresTecnicos.add(1);
    }

    if (DEBUG && (!completada || errorTecnico)) {
        console.log(
            JSON.stringify({
                dataset: DATASET,
                status: respuesta.status,
                origen,
                destino,
                duracion_ms:
                    respuesta.timings.duration,
                body: respuesta.body,
            })
        );
    }
}