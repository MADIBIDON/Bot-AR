# Faire tourner le bot 24h/24 sur un serveur gratuit

Pourquoi : du 16 au 22/09, le bot a été **à l'arrêt 65 % du temps** (100 h sur 153 h)
parce que le Mac se mettait en veille. `caffeinate` limite les dégâts tant que le Mac
est allumé et sur secteur, mais capot fermé sur batterie il dort quand même. Un serveur
toujours allumé règle le problème définitivement.

## 1. Créer le serveur (gratuit) — à faire par toi

Recommandé : **Oracle Cloud "Always Free"**, gratuit sans limite de durée, avec des
datacenters en France (latence minimale vers les boutiques françaises).

1. Crée un compte sur cloud.oracle.com (une carte bancaire est demandée pour vérifier
   l'identité ; les ressources « Always Free » ne sont pas facturées).
2. Région : **France Central (Paris)** ou **France South (Marseille)**.
3. Crée une instance :
   - image : **Ubuntu 24.04**
   - forme : `VM.Standard.E2.1.Micro` (toujours disponible, 1 Go RAM, suffisant)
     ou `VM.Standard.A1.Flex` (ARM, plus puissant, parfois en rupture de capacité)
   - ajoute ta clé SSH publique.
4. Note l'adresse IP publique.

## 2. Arrêter le bot sur le Mac

**Un seul worker à la fois** — deux workers enverraient chaque alerte en double et
auraient deux bases de données qui divergent.

```bash
scripts/uninstall_worker_service.sh
```

## 3. Copier le projet, la config et la base

Depuis le dossier du projet sur le Mac (remplace `IP`) :

```bash
rsync -az --exclude .venv --exclude logs --exclude '__pycache__' ./ ubuntu@IP:/tmp/bot-ar/
ssh ubuntu@IP 'sudo mkdir -p /opt/bot-ar && sudo rsync -a /tmp/bot-ar/ /opt/bot-ar/'
```

`.env` (token Discord, profil de livraison) et `data/app.db` (tes produits, veilles,
historique) sont inclus par le rsync. Ils ne passent **jamais** par git.

## 4. Installer le service

```bash
ssh ubuntu@IP 'bash /opt/bot-ar/deploy/vps/bootstrap.sh'
```

## 5. Vérifier

```bash
ssh ubuntu@IP 'sudo systemctl status retail-worker'
ssh ubuntu@IP 'sudo -u botar /opt/bot-ar/.venv/bin/python /opt/bot-ar/scripts/watch.py status'
```

## Point de vigilance : les IP de datacenter

Certaines boutiques traitent plus sévèrement les IP de datacenter que les IP
résidentielles. Après installation, teste chaque marchand :

```bash
ssh ubuntu@IP 'cd /opt/bot-ar && sudo -u botar .venv/bin/python scripts/watch.py keywords run --keyword-id 1'
```

Si un marchand répond 403 depuis le serveur alors qu'il marche depuis le Mac, il est
bloqué côté IP de datacenter — ce bot ne contourne pas ce type de protection.

## Mettre à jour le code plus tard

```bash
rsync -az --exclude .venv --exclude logs --exclude data --exclude .env ./ ubuntu@IP:/tmp/bot-ar/
ssh ubuntu@IP 'sudo rsync -a /tmp/bot-ar/ /opt/bot-ar/ && sudo chown -R botar:botar /opt/bot-ar && sudo systemctl restart retail-worker'
```

(`data` et `.env` sont exclus ici pour ne pas écraser la base et la config du serveur.)
