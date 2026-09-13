"""Chasse a la capacite Oracle A1 en continu. Pour GitHub Actions.

Strategie a deux niveaux :

1) RAPPORT DE CAPACITE (lecture seule, ne provisionne rien) : on interroge
   ComputeCapacityReport toutes les REPORT_INTERVAL secondes pour savoir si
   la cible (2 OCPU / 12 Go) ou le repli (1 OCPU / 1 Go, le minimum absolu)
   sont disponibles sur l'hyperviseur, SANS jamais tenter de creer la
   machine. C'est un appel different de la creation reelle, donc a priori
   pas soumis a la meme limite de debit -- mais ce n'est pas documente
   noir sur blanc par Oracle, donc on reste prudent (30s de depart) et on
   durcit immediatement au moindre 429 recu sur CET appel precis.
   https://docs.oracle.com/en-us/iaas/tools/python/latest/api/core/models/oci.core.models.ComputeCapacityReport.html

2) CREATION REELLE : on ne lance launch_instance QUE quand le rapport dit
   "disponible" pour une forme donnee. La cadence des tentatives de
   creation elles-memes reste verrouillee a 90s minimum (valeur trouvee
   manuellement et confirmee stable, aucun 429 en dessous de 90s).

Si c'est le repli (1/1) qui est obtenu, le script tente ensuite de le
redimensionner a chaud vers la cible (arret -> resize -> redemarrage).
Aucun risque de perte : si le redimensionnement echoue par manque de
place, l'instance reste a 1 OCPU / 1 Go, fonctionnelle, redimensionnable
manuellement plus tard.

Sortie 0 = session terminee sans capacite (normal, passe le relais au run suivant).
Sortie 1 = INSTANCE OBTENUE (a la cible ou en repli) -> GitHub envoie un mail
           d'echec de workflow, c'est volontaire, c'est la notification immediate.
"""
import os, sys, time, datetime, oci

CONFIG = {
    "user":        os.environ["OCI_USER"],
    "tenancy":     os.environ["OCI_TENANCY"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "region":      os.environ["OCI_REGION"],
    "key_file":    "oci_key.pem",
}
AD     = "Itte:EU-MARSEILLE-1-AD-1"
SUBNET = os.environ["OCI_SUBNET"]
IMAGE  = os.environ["OCI_IMAGE"]
SSHKEY = os.environ["OCI_SSH_KEY"]
TEN    = CONFIG["tenancy"]

# Duree maximale de la session par runner GitHub (300 minutes = 5 heures)
MAX_RUN_MINUTES = int(os.environ.get("MAX_RUN_MINUTES", "300"))
MAX_DURATION    = MAX_RUN_MINUTES * 60

# Cadence du rapport de capacite (lecture seule) -- prudente car non documentee.
REPORT_INTERVAL_START = 30
REPORT_INTERVAL_MAX    = 300
report_interval = REPORT_INTERVAL_START

# Cadence des tentatives de creation reelle -- palier confirme stable.
LAUNCH_MIN_INTERVAL = 90
LAUNCH_MAX_INTERVAL = 900

TARGET   = {"ocpus": 2, "memory_in_gbs": 12, "label": "2 OCPU / 12 Go (cible)"}
FALLBACK = {"ocpus": 1, "memory_in_gbs": 1,  "label": "1 OCPU / 1 Go (repli minimal)"}

cc = oci.core.ComputeClient(CONFIG)
vn = oci.core.VirtualNetworkClient(CONFIG)

def log(msg, end="\n"):
    print(msg, end=end, flush=True)

def make_launch_details(shape_cfg):
    return oci.core.models.LaunchInstanceDetails(
        availability_domain=AD, compartment_id=TEN, display_name="SERV PERSO EVEN",
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
    log(f"  SERVEUR ORACLE OBTENU ({shape_label}) - IP PUBLIQUE : {ip}")
    log(f"  ssh -i ~/.ssh/oracle_mc ubuntu@{ip}")
    log("=" * 60)
    sys.exit(1)  # volontaire : declenche le mail de notification GitHub

def check_capacity_report():
    """Interroge le rapport de capacite pour les deux formes en UN seul appel.
    Retourne (target_available: bool, fallback_available: bool) ou leve
    ServiceError si l'appel echoue (429 y compris)."""
    details = oci.core.models.CreateComputeCapacityReportDetails(
        compartment_id=TEN,
        availability_domain=AD,
        shape_availabilities=[
            oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape="VM.Standard.A1.Flex",
                instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                    ocpus=TARGET["ocpus"], memory_in_gbs=TARGET["memory_in_gbs"])),
            oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                instance_shape="VM.Standard.A1.Flex",
                instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                    ocpus=FALLBACK["ocpus"], memory_in_gbs=FALLBACK["memory_in_gbs"])),
        ],
    )
    report = cc.create_compute_capacity_report(details).data
    avails = report.shape_availabilities
    target_ok   = avails[0].availability_status == "AVAILABLE"
    fallback_ok = avails[1].availability_status == "AVAILABLE"
    return target_ok, fallback_ok

def attempt_launch(shape_cfg, min_interval, max_interval):
    """Tente une creation reelle, avec le meme backoff anti-throttle
    qu'auparavant. Retourne l'instance si obtenue, None si capacite
    perdue entre le rapport et la tentative (race), leve/quitte sur
    erreur fatale."""
    interval = min_interval
    throttles = 0
    while True:
        try:
            inst = cc.launch_instance(make_launch_details(shape_cfg)).data
            return inst
        except oci.exceptions.ServiceError as e:
            msg = (e.message or "").lower()
            if e.status == 429:
                throttles += 1
                interval = min_interval if throttles == 1 else min(int(min_interval * (1.25 ** (throttles - 1))), max_interval)
                wait = min(180 * (2 ** (throttles - 1)), 1800)
                log(f"  [creation] THROTTLE (429 x{throttles}) -> pause {wait}s")
                time.sleep(wait)
                continue
            if e.status == 500 and "capacity" in msg:
                log("  [creation] capacite disparue entre le rapport et la tentative (race perdue).")
                return None
            log(f"  [creation] erreur {e.status} {e.code} : {e.message}")
            return None

def try_upsize_to_target(inst):
    """Tente de faire passer une instance de repli (1/1) a la cible (2/12).
    Sans risque : l'instance existe deja, on ne la perd jamais si ca echoue."""
    log("\n=== Instance de repli obtenue, tentative de redimensionnement vers la cible ===")
    for attempt in range(1, 11):
        try:
            log(f"[redimensionnement #{attempt}] Arret de l'instance...", end=" ")
            cc.instance_action(inst.id, "STOP")
            wait_state(cc.get_instance(inst.id), "STOPPED")
            log("arretee.")

            log(f"[redimensionnement #{attempt}] Application de la nouvelle forme (2 OCPU / 12 Go)...", end=" ")
            cc.update_instance(inst.id, oci.core.models.UpdateInstanceDetails(
                shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                    ocpus=TARGET["ocpus"], memory_in_gbs=TARGET["memory_in_gbs"])))
            log("applique.")

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
    log("Elle n'est pas perdue -- redimensionne-la manuellement depuis la console des que la capacite le permet.")
    return False

# Garde-fou : ne jamais creer une deuxieme machine.
existing = [i for i in cc.list_instances(compartment_id=TEN).data
            if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
if existing:
    inst = existing[0]
    log(f"Instance deja presente : {inst.display_name} [{inst.lifecycle_state}] - rien a faire.")
    sys.exit(0)

start_time = time.time()
log(f"=== DEMARRAGE DE LA CHASSE (session max {MAX_RUN_MINUTES} min) ===")
log(f"Cible : {TARGET['label']} | Repli : {FALLBACK['label']}")
log(f"Rapport de capacite : depart {report_interval}s (non documente, prudent) | Creation reelle : plancher {LAUNCH_MIN_INTERVAL}s (confirme stable)")

n = 0
report_throttles = 0
inst = None
obtained_shape = None

while inst is None:
    elapsed = time.time() - start_time
    if elapsed >= MAX_DURATION:
        log(f"\n[FIN DE SESSION] Duree de {MAX_RUN_MINUTES} min atteinte ({n} verifications).")
        log("Passage de relais propre au prochain workflow GitHub Actions.")
        sys.exit(0)

    n += 1
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    log(f"[{now_str}] Verification #{n} (rapport toutes les {report_interval}s)... ", end="")

    try:
        target_ok, fallback_ok = check_capacity_report()
        report_throttles = 0  # une reponse propre : on peut re-accelerer un peu si on avait ralenti
        if report_interval > REPORT_INTERVAL_START:
            report_interval = max(REPORT_INTERVAL_START, int(report_interval * 0.9))
    except oci.exceptions.ServiceError as e:
        if e.status == 429:
            report_throttles += 1
            report_interval = min(int(report_interval * 1.5), REPORT_INTERVAL_MAX)
            log(f"THROTTLE sur le rapport (429 x{report_throttles}) -> cadence du rapport portee a {report_interval}s")
            time.sleep(report_interval)
            continue
        log(f"erreur rapport {e.status} {e.code} : {e.message} -> nouvel essai dans {report_interval}s")
        time.sleep(report_interval)
        continue
    except Exception as e:
        log(f"exception rapport : {type(e).__name__}: {e} -> nouvel essai dans {report_interval}s")
        time.sleep(report_interval)
        continue

    if target_ok:
        log("CIBLE DISPONIBLE d'apres le rapport -> tentative de creation immediate.")
        inst = attempt_launch(TARGET, LAUNCH_MIN_INTERVAL, LAUNCH_MAX_INTERVAL)
        if inst is not None:
            obtained_shape = TARGET
            log(f"CAPACITE OBTENUE (cible) apres {n} verifications ! ID: {inst.id}")
            break
    elif fallback_ok:
        log("REPLI DISPONIBLE d'apres le rapport -> tentative de creation immediate.")
        inst = attempt_launch(FALLBACK, LAUNCH_MIN_INTERVAL, LAUNCH_MAX_INTERVAL)
        if inst is not None:
            obtained_shape = FALLBACK
            log(f"CAPACITE OBTENUE (repli) apres {n} verifications ! ID: {inst.id}")
            break
    else:
        log("aucune capacite (rapport) pour la cible ni le repli.")

    if (time.time() - start_time) + report_interval >= MAX_DURATION:
        log(f"\n[FIN DE SESSION] Temps restant insuffisant. Total: {n} verifications. Fin propre (exit 0).")
        sys.exit(0)

    time.sleep(report_interval)

log("Attente du passage de l'instance en RUNNING...")
inst = wait_state(cc.get_instance(inst.id), "RUNNING")

if obtained_shape is FALLBACK:
    upsized = try_upsize_to_target(inst)
    final_label = TARGET["label"] if upsized else FALLBACK["label"] + " (a agrandir manuellement plus tard)"
else:
    final_label = TARGET["label"]

announce(inst, final_label)
