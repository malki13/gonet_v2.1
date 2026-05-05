"""Mensajes y constantes del flujo comercial de ventas."""

import httpx

AGENT_NAMES = [
    "Andrea",
    "Andres",
    "Camila",
    "Daniela",
    "Diego",
    "Jorge",
    "Kevin",
    "Luis",
    "Mateo",
    "Mariela",
    "Micaela",
    "Paola",
    "Sofía",
    "Valeria",
]
WELCOME_MSG = "Indíqueme, ¿en qué puedo ayudarle?"
OUT_OF_SCOPE_MSG = (
    "Por ese tema no puedo ayudarle aquí, pero sí con *planes y cobertura* o con *agencias*."
)
INFO_INTRO = (
    "Con gusto le ayudo. ¿Busca información sobre planes o agencias?"
)
DISCOVERY_MSG = (
    "Con gusto le ayudo con planes, cobertura o agencias. "
    "Indíqueme qué está buscando y lo revisamos."
)
LOCATION_PROMPT = (
    "Si lo prefiere, compártame su ubicación de WhatsApp o un link de Google Maps "
    "para ubicar el punto exacto de instalación."
)
LOCATION_FALLBACK_PROMPT = (
    "No pude ubicar esa dirección con seguridad. "
    "¿Me comparte su ubicación de WhatsApp o un link de Google Maps?"
)
AGENCY_PROMPT = "Compártame la ciudad o provincia donde desea buscar una agencia y lo reviso."
PAYMENTS_REDIRECT_MSG = (
    "Si su consulta es sobre pagos, facturas o comprobantes, ingrese por la opción *Soy cliente*. "
    "En el menú principal seleccione *Soy cliente* y ahí encontrará la ayuda para ese trámite."
)
MAIN_MENU_ACTIONS = {
    "type": "buttons",
    "buttons": [
        {"id": "OPCION_CLI", "title": "SOY CLIENTE"},
        {"id": "OPCION_INFORMACION", "title": "INFORMACION"},
    ],
}
MENU = (
    "Si lo prefiere, estas son las opciones disponibles:\n"
    "- Información de planes y cobertura\n"
    "- Información de agencias"
)
MAX_HISTORY_MESSAGES = 8
CRM_FIELD_ORDER = ["partner_name", "city", "street", "phone", "coordinates"]
CRM_QUESTIONS = {
    "partner_name": "Para empezar, ¿me comparte su nombre completo?",
    "city": "Perfecto. ¿En qué ciudad sería la instalación?",
    "street": "Ahora compártame la dirección donde sería la instalación.",
    "phone": "¿A qué número pueden llamarle o escribirle?",
    "coordinates": LOCATION_PROMPT,
}
CRM_CREATED_MSG = "Listo, ya dejé registrada su solicitud."
CRM_PENDING_MSG = "Listo, ya tengo sus datos para seguimiento."
COMMERCIAL_FOLLOWUP_MSG = (
    "Un asesor especializado se pondrá en contacto con usted dentro de las próximas 48 horas."
)
COMMERCIAL_INFO_ONLY_MSG = (
    "Está bien. Igual puedo mostrarle opciones sin solicitarle esos datos. "
    "Si después desea dejarlo avanzado, me avisa."
)
COMMERCIAL_INFO_CHOICE_MSG = (
    "No hace falta que me comparta sus datos todavía. "
    "Si lo prefiere, le muestro todas las opciones o le recomiendo una puntual.\n\n"
    "Indíqueme si prefiere *ver todos los planes* o que le *recomiende uno*."
)
COMMERCIAL_RECOMMENDATION_MSG = (
    "Con gusto le ayudo."
)
COMMERCIAL_RECOMMENDATION_INFO_ONLY_MSG = (
    "Para orientarle, no necesito sus datos."
)
SALES_EXTERNAL_ERRORS = (httpx.HTTPError, RuntimeError, ValueError)

__all__ = [
    "AGENT_NAMES",
    "AGENCY_PROMPT",
    "COMMERCIAL_FOLLOWUP_MSG",
    "COMMERCIAL_INFO_CHOICE_MSG",
    "COMMERCIAL_INFO_ONLY_MSG",
    "COMMERCIAL_RECOMMENDATION_INFO_ONLY_MSG",
    "COMMERCIAL_RECOMMENDATION_MSG",
    "CRM_CREATED_MSG",
    "DISCOVERY_MSG",
    "CRM_FIELD_ORDER",
    "CRM_PENDING_MSG",
    "CRM_QUESTIONS",
    "INFO_INTRO",
    "LOCATION_PROMPT",
    "LOCATION_FALLBACK_PROMPT",
    "MAIN_MENU_ACTIONS",
    "MAX_HISTORY_MESSAGES",
    "MENU",
    "OUT_OF_SCOPE_MSG",
    "PAYMENTS_REDIRECT_MSG",
    "SALES_EXTERNAL_ERRORS",
    "WELCOME_MSG",
]
