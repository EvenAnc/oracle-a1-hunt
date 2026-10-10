"""Chasse a la capacite Oracle A1 en continu. Pour GitHub Actions.

Deux mecanismes en parallele :

1) CREATION REELLE toutes les 90 s, QUOI QU'IL ARRIVE, en alternant la cible
   (2 OCPU / 12 Go) et le repli minimal (1 OCPU / 1 Go). 90 s est le palier
   trouve a la main et confirme stable (aucun 429).

2) RAPPORT DE CAPACITE (lecture seule) toutes les 30 s, comme simple
   ACCELERATEUR : s'il annonce de la place, la creation part tout de suite
   sur la forme annoncee. Il ne bloque JAMAIS une creation : ce rapport est
   connu pour etre faux (https://github.com/oracle/oci-cli/issues/748), on ne
   peut donc pas s'y fier pour decider de ne pas essayer.

Si c'est le repli (1/1) qui est obtenu, le script tente ensuite de le
redimensionner vers la cible (arret -> resize -> redemarrage). Si ca echoue
par manque de place, l'instance reste a 1/1, fonctionnelle, jamais perdue.

Les compteurs de chaque session sont publies en annotation GitHub
(::notice title=STATS::), lisible sans jeton par le tableau de bord.

Sortie 0 = session terminee sans capacite (normal, le run suivant prend le relais).
Sortie 1 = INSTANCE OBTENUE (ou erreur fatale) -> mail d'echec GitHub, volontaire.
"""
import os, sys, time, datetime, signal, oci

CONFIG = {
    "user":        os.environ["OCI_USER"],
    "tenancy":     os.environ["OCI_TENANCY"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "region":      os.environ["OCI_REGION"],
    "key_file":    os.environ.get("OCI_KEY_FILE", "oci_key.pem"),
}
AD     = "Itte:EU-MARSEILLE-1-AD-1"
SUBNET = os.environ["OCI_SUBNET"]
IMAGE  = os.environ["OCI_IMAGE"]
SSHKEY = os.environ["OCI_SSH_KEY"]
TEN    = CONFIG["tenancy"]

MAX_RUN_MINUTES = int(os.environ.get("MAX_RUN_MINUTES", "300"))
MAX_DURATION    = MAX_RUN_MINUTES * 60

REPORT_INTERVAL_START = 30
REPORT_INTERVAL_MAX   = 300
report_interval = REPORT_INTERVAL_START

LAUNCH_MIN_INTERVAL = 90
LAUNCH_MAX_INTERVAL = 900

TARGET   = {"ocpus": 2, "memory_in_gbs": 12, "label": "2 OCPU / 12 Go (cible)"}
FALLBACK = {"ocpus": 1, "memory_in_gbs": 1,  "label": "1 OCPU / 1 Go (repli minimal)"}

STATS = {"verifs": 0, "rapport_dispo": 0, "rapport_429": 0, "creations": 0,
         "creations_aveugles": 0, "creation_429": 0, "pas_de_capacite": 0, "courses_perdues": 0}

cc = oci.core.ComputeClient(CONFIG)
vn = oci.core.VirtualNetworkClient(CONFIG)


def log(msg, end="\n"):
    print(msg, end=end, flush=True)


def emit_stats(reason):
    line = " ".join(f"{k}={v}" for k, v in STATS.items())
    print(f"::notice title=STATS::{line} fin={reason}", flush=True)


def _on_signal(signum, frame):
    emit_stats("interrompu")
    sys.exit(0)


signal.signal(signal.SIGTERM, _on_signal)
signal.signal(signal.SIGINT, _on_signal)


def make_launch_details(shape_cfg):
    return oci.core.models.LaunchInstanceDetails(
        availability_domain=AD, compartment_id=TEN, display_name="serveur-a1",
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=shape_cfg["ocpus"], memory_in_gbs=shape_cfg["memory_in_gbs"]),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=IMAGE, boot_volume_size_in_gbs=150, boot_volume_vpus_per_gb=10),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET, assign_public_ip=True),
        metadata={"ssh_authorized_keys": SSHKEY},
    )


def wait_state(get_fn, target_state, max_wait=1200):
    return oci.wait_until(cc, get_fn, "lifecycle_state", target_state, max_wait_seconds=max_wait).data


def get_public_ip(instance_id):
    for va in cc.list_vnic_attachments(compartment_id=TEN, instance_id=instance_id).data:
        v = vn.get_vnic(va.vnic_id).data
        if v.public_ip:
            return v.public_ip
    return None


def announce(inst, shape_label):
    ip = get_public_ip(inst.id)
    log("=" * 60)
    # Les journaux de ce depot sont publics : ni adresse IP ni identifiant ici.
    log(f"  SERVEUR ORACLE OBTENU ({shape_label}) - IP publique {'attribuee' if ip else 'pas encore attribuee'}")
    log("  Adresse a lire dans la console Oracle : Compute > Instances")
    log("=" * 60)
    sys.exit(1)  # volontaire : declenche le mail de notification GitHub


def check_capacity_report():
    """Un seul appel pour les deux formes. Retourne (cible_ok, repli_ok)."""
    M = oci.core.models
    details = M.CreateComputeCapacityReportDetails(
        compartment_id=TEN,
        availability_domain=AD,
        shape_availabilities=[
            M.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape="VM.Standard.A1.Flex",
                instance_shape_config=M.CapacityReportInstanceShapeConfig(
                    ocpus=cfg["ocpus"], memory_in_gbs=cfg["memory_in_gbs"]))
            for cfg in (TARGET, FALLBACK)
        ],
    )
    avails = cc.create_compute_capacity_report(details).data.shape_availabilities
    return (avails[0].availability_status == "AVAILABLE",
            avails[1].availability_status == "AVAILABLE")


def try_launch(shape_cfg):
    """UNE tentative de creation reelle.
    Retourne (resultat, instance), resultat parmi : ok / capacity / throttle / transient."""
    try:
        return "ok", cc.launch_instance(make_launch_details(shape_cfg)).data
    except oci.exceptions.ServiceError as e:
        msg = (e.message or "").lower()
        if e.status == 429:
            return "throttle", None
        if e.status == 500 and "capacity" in msg:
            return "capacity", None
        if e.status in (401, 500, 502, 503, 504):
            return "transient", None
        log(f"ERREUR NON RECUPERABLE {e.status} {e.code} : {e.message}")
        print(f"::error title=ERREUR_SCRIPT::{e.status} {e.code}", flush=True)
        emit_stats("erreur")
        sys.exit(1)
    except Exception as e:
        log(f"  [creation] exception {type(e).__name__}: {e}")
        return "transient", None


def try_upsize_to_target(inst):
    """Fait passer une instance de repli (1/1) a la cible (2/12).
    Sans risque : l'instance existe deja, on ne la perd jamais si ca echoue."""
    log("=== Instance de repli obtenue, tentative de redimensionnement vers la cible ===")
    for attempt in range(1, 11):
        try:
            log(f"[redimensionnement #{attempt}] Arret de l'instance...", end=" ")
            cc.instance_action(inst.id, "STOP")
            wait_state(cc.get_instance(inst.id), "STOPPED")
            log("arretee.")

            log(f"[redimensionnement #{attempt}] Application de la forme 2 OCPU / 12 Go...", end=" ")
            cc.update_instance(inst.id, oci.core.models.UpdateInstanceDetails(
                shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                    ocpus=TARGET["ocpus"], memory_in_gbs=TARGET["memory_in_gbs"])))
            log("appliquee.")

            log(f"[redimensionnement #{attempt}] Redemarrage...", end=" ")
            cc.instance_action(inst.id, "START")
            wait_state(cc.get_instance(inst.id), "RUNNING")
            log("RUNNING.")
            log("*** Redimensionnement reussi : instance maintenant a 2 OCPU / 12 Go ***")
            return True
        except oci.exceptions.ServiceError as e:
            msg = (e.message or "").lower()
            if e.status == 500 and "capacity" in msg:
                log(f"pas de place pour agrandir pour l'instant (tentative {attempt}/10).")
            else:
                log(f"erreur {e.status} {e.code} : {e.message}")
            try:
                st = cc.get_instance(inst.id).data.lifecycle_state
                if st == "STOPPED":
                    cc.instance_action(inst.id, "START")
                    wait_state(cc.get_instance(inst.id), "RUNNING")
            except Exception:
                pass
            if attempt < 10:
                time.sleep(60)
    log("Redimensionnement impossible pour l'instant : l'instance reste au format minimal.")
    log("Elle n'est pas perdue -- redimensionne-la depuis la console des que la capacite le permet.")
    return False


def end_session(reason):
    log(f"[FIN DE SESSION] {reason} - {STATS['verifs']} verifications, {STATS['creations']} creations tentees.")
    emit_stats("session")
    sys.exit(0)


# Garde-fou : ne jamais creer une deuxieme machine.
existing = [i for i in cc.list_instances(compartment_id=TEN).data
            if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
if existing:
    log(f"Instance deja presente [{existing[0].lifecycle_state}] - rien a faire.")
    print("::notice title=INSTANCE_PRESENTE::une instance existe deja sur le compte", flush=True)
    sys.exit(0)

start_time = time.time()
log(f"=== DEMARRAGE DE LA CHASSE (session max {MAX_RUN_MINUTES} min) ===")
log(f"Cible : {TARGET['label']} | Repli : {FALLBACK['label']}")
log(f"Creation reelle toutes les {LAUNCH_MIN_INTERVAL}s QUOI QUE DISE LE RAPPORT, en alternant cible et repli")
log(f"Rapport de capacite toutes les {report_interval}s : simple accelerateur")

inst = None
obtained_shape = None
launch_interval = LAUNCH_MIN_INTERVAL
launch_throttles = 0
next_launch_at = 0.0
blind_n = 0

while inst is None:
    if time.time() - start_time >= MAX_DURATION:
        end_session(f"duree de {MAX_RUN_MINUTES} min atteinte")

    STATS["verifs"] += 1
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    target_ok = fallback_ok = False
    rapport = "indisponible"
    try:
        target_ok, fallback_ok = check_capacity_report()
        rapport = "CIBLE DISPONIBLE" if target_ok else ("REPLI DISPONIBLE" if fallback_ok else "rien")
        if report_interval > REPORT_INTERVAL_START:
            report_interval = max(REPORT_INTERVAL_START, int(report_interval * 0.9))
    except oci.exceptions.ServiceError as e:
        if e.status == 429:
            STATS["rapport_429"] += 1
            report_interval = min(int(report_interval * 1.5), REPORT_INTERVAL_MAX)
            rapport = f"THROTTLE 429 -> rapport toutes les {report_interval}s"
        else:
            rapport = f"erreur {e.status}"
    except Exception as e:
        rapport = f"exception {type(e).__name__}"
    if target_ok or fallback_ok:
        STATS["rapport_dispo"] += 1

    action = "pas de creation ce tour"
    if time.time() >= next_launch_at:
        blind = not (target_ok or fallback_ok)
        if target_ok:
            shape = TARGET
        elif fallback_ok:
            shape = FALLBACK
        else:
            blind_n += 1
            shape = TARGET if blind_n % 2 == 1 else FALLBACK
        STATS["creations"] += 1
        if blind:
            STATS["creations_aveugles"] += 1
        result, got = try_launch(shape)
        if result == "ok":
            inst, obtained_shape = got, shape
            log(f"[{now_str}] #{STATS['verifs']} rapport: {rapport} | creation {shape['label']} -> OBTENUE !")
            break
        if result == "throttle":
            STATS["creation_429"] += 1
            launch_throttles += 1
            pause = min(180 * (2 ** (launch_throttles - 1)), 1800)
            launch_interval = min(int(LAUNCH_MIN_INTERVAL * (1.25 ** launch_throttles)), LAUNCH_MAX_INTERVAL)
            next_launch_at = time.time() + pause
            action = f"creation {shape['label']} -> THROTTLE 429 (x{launch_throttles}), pause creation {pause}s"
        else:
            if result == "capacity":
                STATS["pas_de_capacite"] += 1
                if not blind:
                    STATS["courses_perdues"] += 1
            launch_throttles = 0
            launch_interval = max(LAUNCH_MIN_INTERVAL, int(launch_interval * 0.95))
            next_launch_at = time.time() + launch_interval
            action = f"creation {shape['label']} -> " + ("pas de capacite" if result == "capacity" else "erreur transitoire")

    log(f"[{now_str}] #{STATS['verifs']} rapport: {rapport} | {action}")

    if (time.time() - start_time) + report_interval >= MAX_DURATION:
        end_session("temps restant insuffisant")
    time.sleep(report_interval)

print(f"::notice title=INSTANCE_OBTENUE::forme={obtained_shape['label']}", flush=True)
emit_stats("obtenue")
log("Attente du passage de l'instance en RUNNING...")
inst = wait_state(cc.get_instance(inst.id), "RUNNING")

if obtained_shape is FALLBACK:
    upsized = try_upsize_to_target(inst)
    final_label = TARGET["label"] if upsized else FALLBACK["label"] + " (a agrandir manuellement plus tard)"
else:
    final_label = TARGET["label"]

announce(inst, final_label)
