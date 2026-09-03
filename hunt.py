"""Chasse a la capacite Oracle A1 en continu. Pour GitHub Actions.

Sortie 0 = session terminee sans capacite (normal, passe le relais au run suivant).
Sortie 1 = INSTANCE OBTENUE -> GitHub envoie un mail d'echec de workflow,
           c'est volontaire, c'est la notification immediate.
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
START_INTERVAL = 110   # point de depart rapide et sur
MIN_INTERVAL   = 85    # plancher de securite anti-429 (Oracle throttle vers 76-80s)
MAX_INTERVAL   = 600   # plafond en cas de throttles repetes

cc = oci.core.ComputeClient(CONFIG)
vn = oci.core.VirtualNetworkClient(CONFIG)

# Garde-fou : ne jamais creer une deuxieme machine.
existing = [i for i in cc.list_instances(compartment_id=TEN).data
            if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
if existing:
    print(f"Instance deja presente : {existing[0].display_name} - rien a faire.", flush=True)
    sys.exit(0)

details = oci.core.models.LaunchInstanceDetails(
    availability_domain=AD, compartment_id=TEN, display_name="SERV PERSO EVEN",
    shape="VM.Standard.A1.Flex",
    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2, memory_in_gbs=12),
    source_details=oci.core.models.InstanceSourceViaImageDetails(
        image_id=IMAGE, boot_volume_size_in_gbs=150, boot_volume_vpus_per_gb=10),
    create_vnic_details=oci.core.models.CreateVnicDetails(
        subnet_id=SUBNET, assign_public_ip=True),
    metadata={"ssh_authorized_keys": SSHKEY},
)

start_time = time.time()
print(f"=== DEMARRAGE DE LA CHASSE GITHUB ACTIONS (session max {MAX_RUN_MINUTES} min) ===", flush=True)
print(f"Config: 2 OCPU / 12 Go / 150 Go | Depart: {START_INTERVAL}s | Plancher: {MIN_INTERVAL}s", flush=True)

n = throttles = n_capacity = propres = 0
interval = START_INTERVAL
inst = None

while inst is None:
    elapsed = time.time() - start_time
    if elapsed >= MAX_DURATION:
        print(f"\n[FIN DE SESSION] Duree de {MAX_RUN_MINUTES} min atteinte ({n} tentatives).", flush=True)
        print("Passage de relais propre au prochain workflow GitHub Actions.", flush=True)
        sys.exit(0)

    n += 1
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{now_str}] Tentative #{n} (cadence: {interval}s)... ", end="", flush=True)

    try:
        inst = cc.launch_instance(details).data
        print(f"\nCAPACITE OBTENUE apres {n} tentatives ! ID: {inst.id}", flush=True)
        break
    except oci.exceptions.ServiceError as e:
        msg = (e.message or "").lower()
        if e.status == 429:
            throttles += 1
            propres = 0
            interval = START_INTERVAL if throttles == 1 else min(int(START_INTERVAL * (1.25 ** (throttles - 1))), MAX_INTERVAL)
            wait = min(180 * (2 ** (throttles - 1)), 1800)
            print(f"THROTTLE (429 x{throttles}) -> pause securite {wait}s, cadence fixee a {interval}s", flush=True)
        elif e.status == 500 and "capacity" in msg:
            throttles = 0
            n_capacity += 1
            propres += 1
            if propres >= 5 and interval > MIN_INTERVAL:
                interval = max(int(interval * 0.85), MIN_INTERVAL)
                propres = 0
                print(f"Pas de capacite (5 propres d'affilee) -> cadence acceleree a {interval}s", flush=True)
            else:
                print(f"Pas de capacite disponible.", flush=True)
            wait = interval
        elif e.status in (401, 500, 502, 503, 504):
            throttles = 0
            wait = interval
            print(f"Erreur transitoire {e.status}.", flush=True)
        else:
            print(f"\nERREUR NON RECUPERABLE {e.status} {e.code} : {e.message}", flush=True)
            sys.exit(1)
    except Exception as e:
        throttles = 0
        wait = interval
        print(f"Exception inattendue : {type(e).__name__}: {e}", flush=True)

    if (time.time() - start_time) + wait >= MAX_DURATION:
        print(f"\n[FIN DE SESSION] Temps restant insuffisant pour attendre {wait}s.", flush=True)
        print(f"Total: {n} tentatives. Fin de session propre (exit 0) pour passer le relais.", flush=True)
        sys.exit(0)

    time.sleep(wait)

print("Attente du passage de l'instance en RUNNING...", flush=True)
inst = oci.wait_until(cc, cc.get_instance(inst.id), "lifecycle_state",
                      "RUNNING", max_wait_seconds=1200).data
ip = None
for va in cc.list_vnic_attachments(compartment_id=TEN, instance_id=inst.id).data:
    v = vn.get_vnic(va.vnic_id).data
    if v.public_ip:
        ip = v.public_ip

print("=" * 60, flush=True)
print(f"  SERVEUR ORACLE OBTENU - IP PUBLIQUE : {ip}", flush=True)
print(f"  ssh -i ~/.ssh/oracle_mc ubuntu@{ip}", flush=True)
print("=" * 60, flush=True)
sys.exit(1)   # volontaire : declenche le mail de notification GitHub
