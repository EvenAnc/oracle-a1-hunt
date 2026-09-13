"""Chasse a la capacite Oracle A1 en continu. Pour GitHub Actions.

Strategie : alterne entre la forme cible (2 OCPU / 12 Go) et une forme de
repli plus petite (1 OCPU / 6 Go), qui a statistiquement plus de chances de
trouver un "trou" libre sur un hyperviseur sature. C'est la recommandation
officielle d'Oracle pour l'erreur "Out of host capacity" :
https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/troubleshooting-out-of-host-capacity.htm

Si c'est la forme de repli qui decroche en premier, le script tente ensuite
de la redimensionner a chaud vers la cible (arret -> resize -> redemarrage).
Si ce redimensionnement echoue par manque de place, l'instance reste a 1/6
en attendant un redimensionnement manuel ulterieur -- elle n'est jamais perdue.

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

# Cadence auto-ajustable
START_INTERVAL = 95    # point de depart proche du palier optimal
MIN_INTERVAL   = 90    # plancher strictement verrouille a 90s (evite tout throttle 429)
MAX_INTERVAL   = 600   # plafond en cas de throttles repetes

# Les deux formes visees, tentees en alternance.
TARGET  = {"ocpus": 2, "memory_in_gbs": 12, "label": "2 OCPU / 12 Go (cible)"}
FALLBACK = {"ocpus": 1, "memory_in_gbs": 6, "label": "1 OCPU / 6 Go (repli)"}

cc = oci.core.ComputeClient(CONFIG)
vn = oci.core.VirtualNetworkClient(CONFIG)

def log(msg, end="\n"):
    print(msg, end=end, flush=True)

def make_details(shape_cfg):
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

def try_upsize_to_target(inst):
    """Tente de faire passer une instance de repli (1/6) a la cible (2/12).
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
            # On s'assure que l'instance est redemarree meme si le resize a echoue,
            # pour ne pas la laisser arretee inutilement.
            try:
                st = cc.get_instance(inst.id).data.lifecycle_state
                if st == "STOPPED":
                    cc.instance_action(inst.id, "START")
                    wait_state(cc.get_instance(inst.id), "RUNNING")
            except Exception:
                pass
            if attempt < 10:
                time.sleep(60)
    log("Redimensionnement impossible pour l'instant : l'instance reste a 1 OCPU / 6 Go.")
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
log(f"=== DEMARRAGE DE LA CHASSE GITHUB ACTIONS (session max {MAX_RUN_MINUTES} min) ===")
log(f"Cible : {TARGET['label']} | Repli : {FALLBACK['label']} | Depart cadence: {START_INTERVAL}s | Plancher: {MIN_INTERVAL}s")

n = throttles = n_capacity = propres = 0
interval = START_INTERVAL
inst = None
obtained_shape = None

while inst is None:
    elapsed = time.time() - start_time
    if elapsed >= MAX_DURATION:
        log(f"\n[FIN DE SESSION] Duree de {MAX_RUN_MINUTES} min atteinte ({n} tentatives).")
        log("Passage de relais propre au prochain workflow GitHub Actions.")
        sys.exit(0)

    n += 1
    # Alternance stricte : une tentative sur deux vise la cible, l'autre le repli.
    shape_cfg = TARGET if n % 2 == 1 else FALLBACK
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    log(f"[{now_str}] Tentative #{n} sur {shape_cfg['label']} (cadence: {interval}s)... ", end="")

    try:
        inst = cc.launch_instance(make_details(shape_cfg)).data
        obtained_shape = shape_cfg
        log(f"\nCAPACITE OBTENUE apres {n} tentatives sur {shape_cfg['label']} ! ID: {inst.id}")
        break
    except oci.exceptions.ServiceError as e:
        msg = (e.message or "").lower()
        if e.status == 429:
            throttles += 1
            propres = 0
            interval = START_INTERVAL if throttles == 1 else min(int(START_INTERVAL * (1.25 ** (throttles - 1))), MAX_INTERVAL)
            wait = min(180 * (2 ** (throttles - 1)), 1800)
            log(f"THROTTLE (429 x{throttles}) -> pause securite {wait}s, cadence fixee a {interval}s")
        elif e.status == 500 and "capacity" in msg:
            throttles = 0
            n_capacity += 1
            propres += 1
            if propres >= 5 and interval > MIN_INTERVAL:
                interval = max(int(interval * 0.85), MIN_INTERVAL)
                propres = 0
                log(f"Pas de capacite (5 propres d'affilee) -> cadence acceleree a {interval}s")
            else:
                log("Pas de capacite disponible.")
            wait = interval
        elif e.status in (401, 500, 502, 503, 504):
            throttles = 0
            wait = interval
            log(f"Erreur transitoire {e.status}.")
        else:
            log(f"\nERREUR NON RECUPERABLE {e.status} {e.code} : {e.message}")
            sys.exit(1)
    except Exception as e:
        throttles = 0
        wait = interval
        log(f"Exception inattendue : {type(e).__name__}: {e}")

    if (time.time() - start_time) + wait >= MAX_DURATION:
        log(f"\n[FIN DE SESSION] Temps restant insuffisant pour attendre {wait}s.")
        log(f"Total: {n} tentatives. Fin de session propre (exit 0) pour passer le relais.")
        sys.exit(0)

    time.sleep(wait)

log("Attente du passage de l'instance en RUNNING...")
inst = wait_state(cc.get_instance(inst.id), "RUNNING")

if obtained_shape is FALLBACK:
    upsized = try_upsize_to_target(inst)
    final_label = TARGET["label"] if upsized else FALLBACK["label"] + " (a agrandir manuellement plus tard)"
else:
    final_label = TARGET["label"]

announce(inst, final_label)
