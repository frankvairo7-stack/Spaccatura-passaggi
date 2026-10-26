import os
import json
from datetime import date, datetime

import pandas as pd
import streamlit as st
from supabase import create_client, Client


# =========================================================
# CONFIGURAZIONE
# =========================================================

st.set_page_config(
    page_title="Spaccatura: passaggi",
    page_icon="🚗",
    layout="wide"
)

st.title("🚗 Spaccatura: passaggi")

st.caption(
    "Registro condiviso dei viaggi, dei chilometri da ricambiare "
    "e delle modifiche effettuate."
)


def get_config():
    try:
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["key"]
    except Exception:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")

    return url, key


url, key = get_config()

if not url or not key:
    st.error(
        "Configurazione Supabase non trovata. "
        "Controlla secrets.toml o le variabili d'ambiente."
    )
    st.stop()


supabase: Client = create_client(url, key)


# =========================================================
# FUNZIONI DI SUPPORTO
# =========================================================

def log_change(actor_name, action, trip_id, description,
               old_data=None, new_data=None):
    """
    Registra un'operazione nella tabella change_log.
    """

    payload = {
        "actor_name": actor_name,
        "action": action,
        "trip_id": str(trip_id) if trip_id is not None else None,
        "description": description,
        "old_data": old_data,
        "new_data": new_data
    }

    supabase.table("change_log").insert(payload).execute()


def trip_snapshot(trip, name_by_id):
    """
    Crea una copia leggibile dei dati del viaggio,
    utile per confrontare la situazione precedente e successiva.
    """

    if trip is None:
        return None

    return {
        "id": str(trip["id"]),
        "data": str(trip["travel_date"]),
        "conducente": name_by_id.get(
            trip["driver_id"], "Sconosciuto"
        ),
        "partenza": trip["origin"],
        "destinazione": trip["destination"],
        "km_auto": float(trip["km"]),
        "note": trip.get("notes") or "",
        "passeggeri": [
            {
                "nome": name_by_id.get(
                    passenger["friend_id"], "Sconosciuto"
                ),
                "km_personali": float(passenger["km"])
            }
            for passenger in trip.get("passengers", [])
        ]
    }


def save_passengers(trip_id, passenger_values, id_by_name):
    """
    Sostituisce i passeggeri di un viaggio con quelli aggiornati.
    """

    supabase.table("trip_passengers").delete().eq(
        "trip_id", trip_id
    ).execute()

    passenger_rows = [
        {
            "trip_id": trip_id,
            "friend_id": id_by_name[name],
            "km": float(km)
        }
        for name, km in passenger_values.items()
    ]

    if passenger_rows:
        supabase.table("trip_passengers").insert(
            passenger_rows
        ).execute()


def format_km(value):
    return (
        f"{value:,.1f}"
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )


# =========================================================
# CARICAMENTO DATI
# =========================================================

@st.cache_data(ttl=10)
def load_friends():
    result = (
        supabase.table("friends")
        .select("id,name,active")
        .eq("active", True)
        .order("created_at")
        .execute()
    )

    return result.data or []


def load_trips():
    result = (
        supabase.table("trips")
        .select(
            "id,travel_date,driver_id,origin,destination,"
            "km,notes,created_at"
        )
        .order("travel_date", desc=True)
        .execute()
    )

    trips_data = result.data or []

    if not trips_data:
        return []

    trip_ids = [trip["id"] for trip in trips_data]

    links = (
        supabase.table("trip_passengers")
        .select("trip_id,friend_id,km")
        .in_("trip_id", trip_ids)
        .execute()
        .data
        or []
    )

    passengers_by_trip = {}

    for link in links:
        passengers_by_trip.setdefault(
            link["trip_id"], []
        ).append({
            "friend_id": link["friend_id"],
            "km": float(link["km"])
        })

    for trip in trips_data:
        trip["passengers"] = passengers_by_trip.get(
            trip["id"], []
        )

    return trips_data


def load_change_log():
    result = (
        supabase.table("change_log")
        .select(
            "id,actor_name,action,trip_id,description,"
            "old_data,new_data,created_at"
        )
        .order("created_at", desc=True)
        .limit(500)
        .execute()
    )

    return result.data or []


try:
    friends = load_friends()

    if len(friends) != 7:
        st.error(
            "Nel database devono esserci esattamente "
            f"7 profili attivi. Attualmente sono: {len(friends)}."
        )
        st.stop()

    name_by_id = {
        friend["id"]: friend["name"]
        for friend in friends
    }

    id_by_name = {
        friend["name"]: friend["id"]
        for friend in friends
    }

    trips = load_trips()

except Exception as exc:
    st.error(f"Errore durante il caricamento dei dati: {exc}")
    st.stop()


# =========================================================
# SCELTA DI CHI STA UTILIZZANDO L'APP
# =========================================================

st.sidebar.header("👤 Chi sei?")

actor_name = st.sidebar.selectbox(
    "Seleziona il tuo nome",
    list(id_by_name.keys()),
    key="current_actor"
)



# =========================================================
# FUNZIONE PER RICARICARE I DATI
# =========================================================

def refresh_app():
    load_friends.clear()
    st.rerun()


# =========================================================
# CALCOLO DEI CHILOMETRI PER OGNI COPPIA
# =========================================================

pair_totals = {}

for trip in trips:
    driver_id = trip["driver_id"]

    for passenger in trip.get("passengers", []):
        passenger_id = passenger["friend_id"]

        if passenger_id == driver_id:
            continue

        pair = (passenger_id, driver_id)

        pair_totals[pair] = (
            pair_totals.get(pair, 0.0)
            + float(passenger["km"])
        )


# =========================================================
# COMPENSAZIONE DEI VIAGGI TRA LE STESSE PERSONE
# =========================================================

net_balances = []
checked_pairs = set()

for person_a, person_b in list(pair_totals.keys()):

    unordered_pair = frozenset((person_a, person_b))

    if unordered_pair in checked_pairs:
        continue

    checked_pairs.add(unordered_pair)

    a_to_b = pair_totals.get(
        (person_a, person_b), 0.0
    )

    b_to_a = pair_totals.get(
        (person_b, person_a), 0.0
    )

    net = a_to_b - b_to_a

    if abs(net) > 0.01:

        if net > 0:
            debtor_id = person_a
            creditor_id = person_b
            km_due = net
        else:
            debtor_id = person_b
            creditor_id = person_a
            km_due = -net

        net_balances.append({
            "Chi deve ricambiare": name_by_id[debtor_id],
            "A chi": name_by_id[creditor_id],
            "Km da ricambiare": round(km_due, 1)
        })


# =========================================================
# RIEPILOGO GENERALE
# =========================================================

col_a, col_b, col_c = st.columns(3)

col_a.metric("Viaggi registrati", len(trips))

total_car_km = sum(
    float(trip["km"])
    for trip in trips
)

col_b.metric(
    "Km totali delle auto",
    format_km(total_car_km)
)

col_c.metric("Profili del gruppo", len(friends))


# =========================================================
# REGISTRAZIONE DI UN NUOVO VIAGGIO
# =========================================================

with st.expander("➕ Registra un nuovo viaggio", expanded=not trips):

    st.write(
        "Inserisci i chilometri complessivi dell'auto e, "
        "per ogni passeggero, i chilometri personali percorsi."
    )

    col1, col2 = st.columns(2)

    with col1:
        travel_date = st.date_input(
            "Data del viaggio",
            value=date.today(),
            key="new_travel_date"
        )

        driver_name = st.selectbox(
            "Chi ha guidato?",
            list(id_by_name.keys()),
            key="new_driver"
        )

        origin = st.text_input(
            "Partenza",
            placeholder="Es. casa di Frank",
            key="new_origin"
        )

        destination = st.text_input(
            "Destinazione finale",
            placeholder="Es. Università di Fisciano",
            key="new_destination"
        )

    with col2:
        total_km = st.number_input(
            "Km complessivi percorsi dall'auto",
            min_value=0.1,
            max_value=3000.0,
            value=30.0,
            step=0.5,
            key="new_total_km"
        )

        notes = st.text_input(
            "Note (facoltative)",
            key="new_notes"
        )

    st.markdown("**Passeggeri e chilometri personali**")
    st.caption(
        "Seleziona chi era in auto. I chilometri personali partono "
        "automaticamente dai chilometri totali dell’auto; puoi "
        "modificarli se qualcuno ha percorso solo una parte del tragitto."
    )

    other_names = [
        name for name in id_by_name
        if name != driver_name
    ]

    passenger_values = {}
    cols = st.columns(2)

    for i, passenger_name in enumerate(other_names):
        with cols[i % 2]:
            friend_id = id_by_name[passenger_name]

            selected = st.checkbox(
                passenger_name,
                key=f"new_passenger_{friend_id}"
            )

            if selected:
                passenger_values[passenger_name] = st.number_input(
                    f"Km percorsi da {passenger_name}",
                    min_value=0.0,
                    max_value=float(total_km),
                    value=float(total_km),
                    step=0.5,
                    key=f"new_km_{friend_id}"
                )

    if st.button(
        "💾 Salva viaggio",
        type="primary",
        use_container_width=True,
        key="save_new_trip"
    ):

        if not origin.strip() or not destination.strip():
            st.error("Inserisci partenza e destinazione.")

        elif not passenger_values:
            st.error("Seleziona almeno un passeggero.")

        elif any(km <= 0 for km in passenger_values.values()):
            st.error(
                "I chilometri di ogni passeggero devono "
                "essere maggiori di zero."
            )

        elif any(
            km > float(total_km)
            for km in passenger_values.values()
        ):
            st.error(
                "I chilometri personali non possono superare "
                "quelli complessivi dell'auto."
            )

        else:
            inserted_trip = None

            try:
                trip_payload = {
                    "travel_date": travel_date.isoformat(),
                    "driver_id": id_by_name[driver_name],
                    "origin": origin.strip(),
                    "destination": destination.strip(),
                    "km": float(total_km),
                    "notes": notes.strip()
                }

                inserted_trip = (
                    supabase.table("trips")
                    .insert(trip_payload)
                    .execute()
                    .data
                )

                if not inserted_trip:
                    raise RuntimeError(
                        "Supabase non ha restituito il viaggio inserito."
                    )

                trip_id = inserted_trip[0]["id"]

                passenger_rows = [
                    {
                        "trip_id": trip_id,
                        "friend_id": id_by_name[name],
                        "km": float(km)
                    }
                    for name, km in passenger_values.items()
                ]

                supabase.table("trip_passengers").insert(
                    passenger_rows
                ).execute()

                new_snapshot = {
                    "data": travel_date.isoformat(),
                    "conducente": driver_name,
                    "partenza": origin.strip(),
                    "destinazione": destination.strip(),
                    "km_auto": float(total_km),
                    "note": notes.strip(),
                    "passeggeri": [
                        {
                            "nome": name,
                            "km_personali": float(km)
                        }
                        for name, km in passenger_values.items()
                    ]
                }

                log_change(
                    actor_name=actor_name,
                    action="Aggiunta",
                    trip_id=trip_id,
                    description=(
                        f"Nuovo viaggio: {origin.strip()} → "
                        f"{destination.strip()}"
                    ),
                    old_data=None,
                    new_data=new_snapshot
                )

                st.success("Viaggio salvato e registrato nel registro!")
                refresh_app()

            except Exception as exc:
                st.error(
                    "Errore durante il salvataggio. "
                    "Controlla anche su Supabase se il viaggio "
                    f"è stato inserito. Dettaglio: {exc}"
                )


# =========================================================
# MODIFICA ED ELIMINAZIONE DEI VIAGGI
# =========================================================

st.divider()
with st.expander("✏️ Modifica o elimina un viaggio", expanded=False):
    if not trips:
        st.info("Non ci sono viaggi da modificare o eliminare.")

    else:
        trip_labels = {}

        for trip in trips:
            label = (
                f"{trip['travel_date']} | "
                f"{trip['origin']} → {trip['destination']} | "
                f"{name_by_id.get(trip['driver_id'], 'Sconosciuto')} | "
                f"{trip['km']} km"
            )
            trip_labels[label] = trip

        selected_label = st.selectbox(
            "Scegli il viaggio",
            list(trip_labels.keys()),
            key="edit_trip_selection"
        )

        selected_trip = trip_labels[selected_label]
        selected_trip_id = selected_trip["id"]

        original_snapshot = trip_snapshot(selected_trip, name_by_id)

        st.caption(
            f"Viaggio selezionato: {selected_trip['origin']} → "
            f"{selected_trip['destination']}."
        )

        edit_tab, delete_tab = st.tabs(
            ["✏️ Modifica viaggio", "🗑️ Elimina viaggio"]
        )

        with edit_tab:
            st.write("Aggiorna i campi e salva le modifiche.")

            with st.form(
                key=f"edit_form_{selected_trip_id}"
            ):
                col1, col2 = st.columns(2)

                with col1:
                    edit_date = st.date_input(
                        "Data",
                        value=date.fromisoformat(
                            str(selected_trip["travel_date"])[:10]
                        ),
                        key=f"edit_date_{selected_trip_id}"
                    )

                    current_driver_name = name_by_id[
                        selected_trip["driver_id"]
                    ]

                    edit_driver = st.selectbox(
                        "Conducente",
                        list(id_by_name.keys()),
                        index=list(id_by_name.keys()).index(
                            current_driver_name
                        ),
                        key=f"edit_driver_{selected_trip_id}"
                    )

                    edit_origin = st.text_input(
                        "Partenza",
                        value=selected_trip["origin"],
                        key=f"edit_origin_{selected_trip_id}"
                    )

                    edit_destination = st.text_input(
                        "Destinazione",
                        value=selected_trip["destination"],
                        key=f"edit_destination_{selected_trip_id}"
                    )

                with col2:
                    edit_total_km = st.number_input(
                        "Km complessivi dell'auto",
                        min_value=0.1,
                        max_value=3000.0,
                        value=float(selected_trip["km"]),
                        step=0.5,
                        key=f"edit_total_km_{selected_trip_id}"
                    )

                    edit_notes = st.text_input(
                        "Note",
                        value=selected_trip.get("notes") or "",
                        key=f"edit_notes_{selected_trip_id}"
                    )

                st.markdown("**Passeggeri e chilometri personali**")
                st.caption(
                    "Seleziona i passeggeri e modifica i chilometri individuali "
                    "se non hanno percorso l’intero tragitto."
                )

                edit_passengers = {}
                current_passenger_map = {
                    name_by_id[p["friend_id"]]: float(p["km"])
                    for p in selected_trip.get("passengers", [])
                    if p["friend_id"] in name_by_id
                }

                edit_other_names = [
                    name for name in id_by_name
                    if name != edit_driver
                ]

                edit_cols = st.columns(2)

                for i, passenger_name in enumerate(edit_other_names):
                    with edit_cols[i % 2]:
                        friend_id = id_by_name[passenger_name]
                        was_selected = passenger_name in current_passenger_map

                        is_selected = st.checkbox(
                            passenger_name,
                            value=was_selected,
                            key=f"edit_passenger_{selected_trip_id}_{friend_id}"
                        )

                        if is_selected:
                            default_km = current_passenger_map.get(
                                passenger_name,
                                float(edit_total_km)
                            )

                            default_km = min(
                                float(edit_total_km),
                                max(0.1, default_km)
                            )

                            edit_passengers[passenger_name] = st.number_input(
                                f"Km personali di {passenger_name}",
                                min_value=0.0,
                                max_value=float(edit_total_km),
                                value=default_km,
                                step=0.5,
                                key=f"edit_km_{selected_trip_id}_{friend_id}"
                            )

                save_edit = st.form_submit_button(
                    "💾 Salva modifiche",
                    type="primary",
                    use_container_width=True
                )

            if save_edit:
                if not edit_origin.strip() or not edit_destination.strip():
                    st.error("Partenza e destinazione sono obbligatorie.")

                elif not edit_passengers:
                    st.error("Seleziona almeno un passeggero.")

                elif any(km <= 0 for km in edit_passengers.values()):
                    st.error(
                        "I chilometri personali dei passeggeri "
                        "devono essere maggiori di zero."
                    )

                elif any(
                    km > float(edit_total_km)
                    for km in edit_passengers.values()
                ):
                    st.error(
                        "I chilometri personali non possono superare "
                        "i chilometri totali dell'auto."
                    )

                else:
                    try:
                        updated_payload = {
                            "travel_date": edit_date.isoformat(),
                            "driver_id": id_by_name[edit_driver],
                            "origin": edit_origin.strip(),
                            "destination": edit_destination.strip(),
                            "km": float(edit_total_km),
                            "notes": edit_notes.strip()
                        }

                        supabase.table("trips").update(
                            updated_payload
                        ).eq("id", selected_trip_id).execute()

                        save_passengers(
                            selected_trip_id,
                            edit_passengers,
                            id_by_name
                        )

                        new_snapshot = {
                            "data": edit_date.isoformat(),
                            "conducente": edit_driver,
                            "partenza": edit_origin.strip(),
                            "destinazione": edit_destination.strip(),
                            "km_auto": float(edit_total_km),
                            "note": edit_notes.strip(),
                            "passeggeri": [
                                {
                                    "nome": name,
                                    "km_personali": float(km)
                                }
                                for name, km in edit_passengers.items()
                            ]
                        }

                        log_change(
                            actor_name=actor_name,
                            action="Modifica",
                            trip_id=selected_trip_id,
                            description=(
                                f"Modificato viaggio: "
                                f"{edit_origin.strip()} → "
                                f"{edit_destination.strip()}"
                            ),
                            old_data=original_snapshot,
                            new_data=new_snapshot
                        )

                        st.success(
                            "Viaggio aggiornato! I debiti verranno "
                            "ricalcolati al ricaricamento."
                        )
                        refresh_app()

                    except Exception as exc:
                        st.error(
                            "Errore durante la modifica. "
                            "Controlla il viaggio su Supabase prima "
                            f"di riprovare. Dettaglio: {exc}"
                        )

        with delete_tab:
            st.warning(
                "L'eliminazione rimuoverà il viaggio e i relativi "
                "passeggeri. I debiti verranno ricalcolati."
            )

            confirm_delete = st.checkbox(
                "Confermo di voler eliminare questo viaggio.",
                key=f"confirm_delete_{selected_trip_id}"
            )

            if st.button(
                "🗑️ Elimina definitivamente",
                type="primary",
                disabled=not confirm_delete,
                key=f"delete_trip_{selected_trip_id}"
            ):
                try:
                    # Prima eliminiamo i collegamenti ai passeggeri.
                    supabase.table("trip_passengers").delete().eq(
                        "trip_id", selected_trip_id
                    ).execute()

                    # Poi eliminiamo il viaggio.
                    supabase.table("trips").delete().eq(
                        "id", selected_trip_id
                    ).execute()

                    # Registriamo l'eliminazione con una copia dei dati.
                    log_change(
                        actor_name=actor_name,
                        action="Eliminazione",
                        trip_id=selected_trip_id,
                        description=(
                            f"Eliminato viaggio: "
                            f"{selected_trip['origin']} → "
                            f"{selected_trip['destination']}"
                        ),
                        old_data=original_snapshot,
                        new_data=None
                    )

                    st.success(
                        "Viaggio eliminato. Il registro conserva "
                        "la descrizione e i dati precedenti."
                    )
                    refresh_app()

                except Exception as exc:
                    st.error(
                        "Errore durante l'eliminazione. "
                        "Verifica lo stato del viaggio su Supabase. "
                        f"Dettaglio: {exc}"
                    )


    # =========================================================
# STORICO DEI VIAGGI
# =========================================================

st.divider()
st.subheader("📋 Viaggi registrati")

if trips:
    trip_rows = []

    for trip in trips:
        passengers_text = ", ".join(
            (
                f"{name_by_id.get(p['friend_id'], 'Sconosciuto')} "
                f"({p['km']:g} km)"
            )
            for p in trip.get("passengers", [])
        )

        trip_rows.append({
            "Data": trip["travel_date"],
            "Conducente": name_by_id.get(
                trip["driver_id"], "Sconosciuto"
            ),
            "Partenza": trip["origin"],
            "Destinazione": trip["destination"],
            "Km auto": float(trip["km"]),
            "Passeggeri (km personali)": passengers_text,
            "Note": trip.get("notes") or ""
        })

    st.dataframe(
        pd.DataFrame(trip_rows),
        use_container_width=True,
        hide_index=True
    )

else:
    st.info("Non ci sono ancora viaggi registrati.")


# =========================================================
# CHILOMETRI DA RICAMBIARE TRA LE COPPIE
# =========================================================

st.subheader("🔄 Chilometri da ricambiare")

st.caption(
    "I viaggi in direzione opposta compensano il saldo "
    "della stessa coppia."
)

if net_balances:
    st.dataframe(
        pd.DataFrame(net_balances),
        use_container_width=True,
        hide_index=True
    )
else:
    st.info(
        "Al momento non risultano chilometri da ricambiare."
    )


# =========================================================
# RIEPILOGO INDIVIDUALE
# =========================================================

st.divider()
st.subheader("👥 Riepilogo individuale")

summary = {
    friend["name"]: {
        "Km da ricambiare": 0.0,
        "Km da ricevere": 0.0
    }
    for friend in friends
}

for balance in net_balances:
    debtor = balance["Chi deve ricambiare"]
    creditor = balance["A chi"]
    km_due = float(balance["Km da ricambiare"])

    summary[debtor]["Km da ricambiare"] += km_due
    summary[creditor]["Km da ricevere"] += km_due

summary_rows = []

for friend in friends:
    name = friend["name"]

    km_to_give = summary[name]["Km da ricambiare"]
    km_to_receive = summary[name]["Km da ricevere"]

    saldo_netto = km_to_receive - km_to_give

    if saldo_netto > 0.01:
        situazione = "Deve ricevere"
    elif saldo_netto < -0.01:
        situazione = "Deve ricambiare"
    else:
        situazione = "In pareggio"

    summary_rows.append({
        "Amico": name,
        "Km da ricambiare": round(km_to_give, 1),
        "Km da ricevere": round(km_to_receive, 1),
        "Saldo netto": round(saldo_netto, 1),
        "Situazione": situazione
    })

summary_df = pd.DataFrame(summary_rows)

st.dataframe(
    summary_df,
    use_container_width=True,
    hide_index=True
)


# =========================================================
# STATISTICHE DEL GRUPPO
# =========================================================

st.divider()
st.subheader("📊 Statistiche del gruppo")
st.caption(
    "Le statistiche distinguono i chilometri percorsi alla guida "
    "dai chilometri personali percorsi come passeggero."
)

# Filtro temporale: tutti i periodi oppure un singolo mese.
available_months = sorted({
    str(trip["travel_date"])[:7]
    for trip in trips
    if trip.get("travel_date")
}, reverse=True)

month_options = ["Tutti i periodi"] + available_months
stats_col1, stats_col2 = st.columns([1, 1])

with stats_col1:
    selected_month = st.selectbox(
        "Periodo",
        month_options,
        key="stats_month"
    )

with stats_col2:
    selected_person = st.selectbox(
        "Persona",
        ["Tutto il gruppo"] + list(id_by_name.keys()),
        key="stats_person"
    )

filtered_trips = [
    trip for trip in trips
    if selected_month == "Tutti i periodi"
    or str(trip.get("travel_date", ""))[:7] == selected_month
]

stats_by_person = {
    friend["name"]: {
        "Viaggi alla guida": 0,
        "Km alla guida": 0.0,
        "Passaggi dati": 0,
        "Viaggi da passeggero": 0,
        "Km da passeggero": 0.0
    }
    for friend in friends
}

for trip in filtered_trips:
    driver_name = name_by_id.get(trip["driver_id"])
    if driver_name in stats_by_person:
        stats_by_person[driver_name]["Viaggi alla guida"] += 1
        stats_by_person[driver_name]["Km alla guida"] += float(trip["km"])

    for passenger in trip.get("passengers", []):
        passenger_name = name_by_id.get(passenger["friend_id"])
        if passenger_name in stats_by_person:
            stats_by_person[passenger_name]["Passaggi dati"] += 1
            stats_by_person[passenger_name]["Viaggi da passeggero"] += 1
            stats_by_person[passenger_name]["Km da passeggero"] += float(
                passenger["km"]
            )

stats_rows = []
for person_name, values in stats_by_person.items():
    stats_rows.append({
        "Persona": person_name,
        "Viaggi alla guida": values["Viaggi alla guida"],
        "Km alla guida": round(values["Km alla guida"], 1),
        "Passaggi dati": values["Passaggi dati"],
        "Viaggi da passeggero": values["Viaggi da passeggero"],
        "Km da passeggero": round(values["Km da passeggero"], 1)
    })

stats_df = pd.DataFrame(stats_rows)

if selected_person != "Tutto il gruppo":
    stats_df = stats_df[stats_df["Persona"] == selected_person]

if not filtered_trips:
    st.info("Non ci sono viaggi registrati nel periodo selezionato.")
else:
    st.dataframe(
        stats_df,
        use_container_width=True,
        hide_index=True
    )

    if selected_person == "Tutto il gruppo":
        st.markdown("**Classifiche del periodo**")

        chart_col1, chart_col2 = st.columns(2)

        with chart_col1:
            st.caption("Chilometri percorsi alla guida")
            driver_chart = (
                stats_df[["Persona", "Km alla guida"]]
                .set_index("Persona")
                .sort_values("Km alla guida", ascending=False)
            )
            st.bar_chart(driver_chart)

        with chart_col2:
            st.caption("Chilometri personali da passeggero")
            passenger_chart = (
                stats_df[["Persona", "Km da passeggero"]]
                .set_index("Persona")
                .sort_values("Km da passeggero", ascending=False)
            )
            st.bar_chart(passenger_chart)
    else:
        person_stats = stats_by_person[selected_person]
        metric_col1, metric_col2, metric_col3 = st.columns(3)
        metric_col1.metric(
            "Viaggi alla guida",
            person_stats["Viaggi alla guida"]
        )
        metric_col2.metric(
            "Km alla guida",
            format_km(person_stats["Km alla guida"])
        )
        metric_col3.metric(
            "Km da passeggero",
            format_km(person_stats["Km da passeggero"])
        )

    st.caption(
        "Nota: 'Passaggi dati' conta le presenze dei passeggeri nei viaggi "
        "registrati; non rappresenta un rimborso economico."
    )


# =========================================================
# CHI DOVREBBE PRENDERE LA MACCHINA
# =========================================================

st.divider()
with st.expander("🚗 Chi dovrebbe prendere la macchina?", expanded=False):
    st.caption(
        "I suggerimenti si basano sui debiti ancora aperti "
        "tra le singole coppie."
    )

    if net_balances:
        sorted_balances = sorted(
            net_balances,
            key=lambda row: float(row["Km da ricambiare"]),
            reverse=True
        )

        recommendation_rows = []

        for index, balance in enumerate(sorted_balances, start=1):
            recommendation_rows.append({
                "Priorità": index,
                "Chi dovrebbe guidare": balance["Chi deve ricambiare"],
                "Chi dovrebbe portare": balance["A chi"],
                "Km personali da compensare": balance["Km da ricambiare"]
            })

        st.dataframe(
            pd.DataFrame(recommendation_rows),
            use_container_width=True,
            hide_index=True
        )

        st.info(
            "Quando organizzate il prossimo viaggio, potete provare "
            "a far guidare la persona indicata e a portare il passeggero "
            "associato. Registrate i chilometri effettivamente percorsi."
        )

    else:
        st.success(
            "Non ci sono debiti da compensare."
        )


    # =========================================================
# SPIEGAZIONE DEL SALDO
# =========================================================

st.markdown("**Come leggere il riepilogo**")

st.markdown(
    """
    - **Km da ricambiare:** chilometri che la persona deve restituire.
    - **Km da ricevere:** chilometri che gli altri devono restituire.
    - **Saldo netto positivo:** la persona deve ricevere più chilometri.
    - **Saldo netto negativo:** la persona deve ricambiare più chilometri.
    - **In pareggio:** i due valori si equivalgono.
    """
)


# =========================================================
# REGISTRO DELLE MODIFICHE
# =========================================================

st.divider()
with st.expander("📜 Registro delle modifiche", expanded=False):
    st.caption(
        "Sono mostrate le ultime 500 operazioni registrate. "
        "Il registro include aggiunte, modifiche ed eliminazioni."
    )

    try:
        change_log = load_change_log()

        if change_log:
            log_rows = []

            for entry in change_log:
                created_at = entry.get("created_at")

                if created_at:
                    try:
                        created_at = datetime.fromisoformat(
                            created_at.replace("Z", "+00:00")
                        ).astimezone().strftime("%d/%m/%Y %H:%M:%S")
                    except Exception:
                        created_at = str(created_at)

                log_rows.append({
                    "Data e ora": created_at or "",
                    "Chi": entry.get("actor_name", ""),
                    "Operazione": entry.get("action", ""),
                    "Viaggio ID": entry.get("trip_id") or "",
                    "Descrizione": entry.get("description", "")
                })

            st.dataframe(
                pd.DataFrame(log_rows),
                use_container_width=True,
                hide_index=True
            )

            with st.expander("🔎 Visualizza i dettagli delle modifiche"):
                for entry in change_log:
                    created_at = entry.get("created_at", "")

                    st.markdown(
                        f"**{entry.get('action', '')}** — "
                        f"{entry.get('actor_name', '')} — "
                        f"{created_at}"
                    )

                    st.write(entry.get("description", ""))

                    old_data = entry.get("old_data")
                    new_data = entry.get("new_data")

                    if old_data is not None:
                        st.markdown("**Dati precedenti**")
                        st.json(old_data)

                    if new_data is not None:
                        st.markdown("**Dati successivi**")
                        st.json(new_data)

                    st.divider()

        else:
            st.info(
                "Il registro è vuoto. Le nuove operazioni "
                "compariranno qui."
            )

    except Exception as exc:
        st.error(
            "Non riesco a leggere il registro. Controlla che la tabella "
            f"change_log esista e che le autorizzazioni siano corrette. "
            f"Dettaglio: {exc}"
        )


    # =========================================================
# ESPORTAZIONE E BACKUP
# =========================================================

st.divider()
with st.expander("📥 Esporta dati e backup", expanded=False):
    st.caption(
        "Scarica copie CSV dello storico, dei debiti e del riepilogo."
    )

    trip_export_rows = []

    for trip in trips:
        passengers_text = ", ".join(
            (
                f"{name_by_id.get(p['friend_id'], 'Sconosciuto')} "
                f"({p['km']:g} km)"
            )
            for p in trip.get("passengers", [])
        )

        trip_export_rows.append({
            "Data": trip["travel_date"],
            "Conducente": name_by_id.get(
                trip["driver_id"], "Sconosciuto"
            ),
            "Partenza": trip["origin"],
            "Destinazione": trip["destination"],
            "Km auto": float(trip["km"]),
            "Passeggeri e km personali": passengers_text,
            "Note": trip.get("notes") or ""
        })

    trip_export_df = pd.DataFrame(
        trip_export_rows,
        columns=[
            "Data",
            "Conducente",
            "Partenza",
            "Destinazione",
            "Km auto",
            "Passeggeri e km personali",
            "Note"
        ]
    )

    trip_csv = trip_export_df.to_csv(
        index=False,
        sep=";",
        encoding="utf-8-sig"
    )

    debts_export_df = pd.DataFrame(
        net_balances,
        columns=[
            "Chi deve ricambiare",
            "A chi",
            "Km da ricambiare"
        ]
    )

    debts_csv = debts_export_df.to_csv(
        index=False,
        sep=";",
        encoding="utf-8-sig"
    )

    summary_csv = summary_df.to_csv(
        index=False,
        sep=";",
        encoding="utf-8-sig"
    )

    col_export1, col_export2, col_export3 = st.columns(3)

    with col_export1:
        st.download_button(
            label="📋 Scarica storico viaggi",
            data=trip_csv,
            file_name="storico_viaggi.csv",
            mime="text/csv",
            use_container_width=True
        )

    with col_export2:
        st.download_button(
            label="🔄 Scarica debiti tra coppie",
            data=debts_csv,
            file_name="debiti_tra_amici.csv",
            mime="text/csv",
            use_container_width=True
        )

    with col_export3:
        st.download_button(
            label="👥 Scarica riepilogo individuale",
            data=summary_csv,
            file_name="riepilogo_individuale.csv",
            mime="text/csv",
            use_container_width=True
        )

    st.caption(
        "Prototipo aggiornato. I chilometri sono un'unità convenzionale "
        "per ricambiare i passaggi, non un calcolo del costo reale "
        "del carburante."
    )
