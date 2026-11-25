# File d'attente, reprise après limite et agent local

Trois fonctions ajoutées à Open WebUI :

1. **Reprise automatique après une limite** : quand Claude atteint sa limite, un message est
   renvoyé tout seul dès qu'elle est levée.
2. **File d'attente de conversations avec priorités** : vous empilez des conversations, elles
   s'exécutent une par une, de la plus prioritaire à la moins prioritaire, même si vous avez
   fermé l'onglet.
3. **Notifications** : limite atteinte, une conversation terminée (avec aperçu de la réponse et
   fichiers modifiés), toute la file terminée, validation requise, échec.
4. **Agent local** (« comme Claude Code ») : le modèle peut lire/modifier des fichiers, lancer des
   commandes et démarrer des serveurs sur votre ordinateur.

---

## 1. Démarrage rapide

> **L'agent local agit sur la machine qui exécute le backend.** Sous Docker, il agirait _dans le
> conteneur_, pas sur votre PC. Lancez donc le backend directement sur votre machine
> (`pip install open-webui` / `backend/dev.sh` / `uvicorn`), ou montez votre dossier de projet
> dans le conteneur.

> **Sans installer Python à la main :** les exécutables Windows/Linux construits par la CI
> (voir le README, section « Téléchargement ») installent et lancent l'application. Pour activer
> l'agent local avec eux : `BetterOpenWebUI --agent "C:\\Users\\moi\\projets"` (seul ce dossier est
> autorisé, serveur limité à votre ordinateur).

Variables d'environnement (voir aussi `.env.example`) :

| Variable                      | Rôle                                                     | Défaut                  |
| ----------------------------- | -------------------------------------------------------- | ----------------------- |
| `ENABLE_CHAT_QUEUE`           | active la file d'attente                                 | `true`                  |
| `ENABLE_LOCAL_AGENT`          | **active l'agent local**                                 | `false`                 |
| `LOCAL_AGENT_ALLOWED_ROOTS`   | dossiers autorisés (`:` Linux/macOS, `;` Windows)        | votre dossier personnel |
| `LOCAL_AGENT_ADMIN_ONLY`      | réserve l'agent aux administrateurs                      | `true`                  |
| `LOCAL_AGENT_COMMAND_TIMEOUT` | durée max d'une commande (s)                             | `120`                   |
| `CHAT_QUEUE_LANG`             | langue des notifications envoyées au webhook (`fr`/`en`) | `fr`                    |

Exemple (Linux/macOS) :

```bash
export ENABLE_LOCAL_AGENT=true
export LOCAL_AGENT_ALLOWED_ROOTS="$HOME/projets"
open-webui serve
```

Les mêmes réglages sont modifiables ensuite dans **File d'attente → Réglages** (section
administration). Connexion à Claude : _Admin → Paramètres → Connexions → OpenAI_, URL
`https://api.anthropic.com/v1`, votre clé API.

---

## 2. Reprise automatique après une limite

Trois façons de l'armer :

- **Bouton sous le message d'erreur** : quand une réponse s'arrête sur une limite, un bandeau
  propose **« Reprendre automatiquement »**. Un clic = la reprise est programmée.
- **Bouton dans la barre de saisie** (icône liste) : interrupteur **« Reprendre automatiquement
  après une limite »**. Activé, toute limite rencontrée programme la reprise sans rien faire.
  Le bouton affiche le compte à rebours quand une limite est active.
- **File d'attente → Réglages**.

Comment ça marche :

- L'heure de fin de limite est lue dans l'erreur du fournisseur : en-tête `retry-after`,
  en-têtes `anthropic-ratelimit-*-reset`, date ISO (« You will regain access on … »), durée
  (« try again in 4 hours »), heure (« resets at 5pm (Europe/Paris) »), format Claude Code.
- **Si aucune heure n'est donnée**, l'attente n'est pas aveugle : la file _teste_ à intervalles
  croissants (10 min → 30 min max) et reprend dès que la limite saute.
- Une heure exacte l'emporte toujours sur une estimation.
- Si la réponse a échoué **avant d'écrire quoi que ce soit**, la _même question_ est reposée
  (pas de doublon). Si du travail avait déjà commencé, le message « Continue exactement où tu
  t'es arrêté… » est envoyé (modifiable).
- Pendant la limite, **rien n'est envoyé** ; le bouton **« La limite est levée — reprendre
  maintenant »** permet de forcer.

---

## 3. File d'attente (page _File d'attente_ du menu)

- **Ajouter** : nouvelle conversation (titre, modèle, message) ou message dans une conversation
  existante.
- **Priorité** : Haute / Normale / Basse à la création, puis flèches ↑↓ pour réordonner,
  « Passer en premier », pause d'un élément, modification, annulation, nouvelle tentative.
- **Exécution** : un élément à la fois, dans l'ordre. Une reprise après limite passe devant.
  Si vous écrivez vous-même dans la conversation visée, la file attend (30 s) au lieu de s'y
  mêler.
- **Mettre toute la file en pause** / la relancer.
- Les éléments terminés sont dans « Terminées » (aperçu, ouverture de la conversation, relance).

### Fonctionne sans navigateur

La file est exécutée par le serveur (même chemin que le chat : filtres, outils, mémoire…). Fermer
l'onglet n'arrête rien ; il faut en revanche que le **serveur reste allumé**. Au redémarrage,
l'élément interrompu est remis en file.

---

## 4. Notifications

| Événement                 | Quand                                                            |
| ------------------------- | ---------------------------------------------------------------- |
| **Limite atteinte**       | une limite arrête la file (heure de reprise + nombre en attente) |
| **Conversation terminée** | une réponse est finie (aperçu + nombre de fichiers modifiés)     |
| **File terminée**         | plus rien à exécuter (X terminées, Y en échec)                   |
| **Validation requise**    | l'assistant veut exécuter une action que vous devez autoriser    |
| **Échec**                 | erreur autre qu'une limite                                       |

Canaux : notification dans l'application (clic = ouvre la conversation), **notification du
navigateur** (si l'onglet est en arrière-plan), **son**, et **webhook** (Discord, Slack, ntfy…)
si vous avez défini une cible dans _Paramètres → Notifications_ : c'est ce qui arrive sur votre
téléphone. Chaque type se désactive dans _File d'attente → Réglages_. Pour les notifications du
navigateur : bouton « Activer les notifications du navigateur » (même page).

---

## 5. Agent local

Active, l'icône **terminal** apparaît dans la barre de saisie. Elle ouvre : l'interrupteur, le
**dossier de travail** et le **mode de validation**.

Outils donnés au modèle : `local_list_dir`, `local_glob`, `local_search` (recherche regex),
`local_read_file`, `local_edit_file` (remplacement exact + diff, conserve CRLF/LF),
`local_write_file`, `local_run_command`, `local_start_process` / `local_process_logs` /
`local_stop_process` (serveurs de dev, watchers). Le modèle reçoit aussi des consignes de travail
(explorer avant de modifier, lire avant d'éditer, vérifier avec les tests, etc.).

### Modes de validation

| Mode                                         | Comportement                                                                                                         |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| **Tout valider** (défaut)                    | chaque modification et commande demande votre accord (Approuver / Refuser dans le chat)                              |
| **Modifications libres, commandes validées** | les fichiers sont modifiés sans demander, les commandes demandent                                                    |
| **Autonome**                                 | rien ne demande — nécessaire pour une file d'attente vraiment sans surveillance ; à choisir en connaissance de cause |

La **lecture** (lister, lire, chercher) ne demande jamais. Les commandes d'inspection simples
(`ls`, `git status`, `git diff`, `cat fichier`…) non plus. Dans la file d'attente, une action
à valider met l'élément en « Validation requise » (vous êtes notifié) et **le reste de la file
continue** ; approuvez dans la conversation pour terminer l'élément.

### Ce qui vous protège — et ce qui ne vous protège pas

Protégé :

- fonction désactivée par défaut, réservée aux administrateurs, limitée aux dossiers autorisés ;
- les outils de fichiers ne peuvent pas sortir de ces dossiers (liens symboliques et `..`
  compris) ni toucher `.ssh`, `.aws`, `.gnupg`… ;
- liste noire toujours active, même en mode autonome : `rm -rf /` ou `~` ou `*`, `sudo`/`su`,
  `mkfs`/`dd` sur disque, `shutdown`/`reboot`, `curl … | sh`, bombe fork, formatage de disque ;
- les secrets du serveur (clés API, `WEBUI_SECRET_KEY`, jetons…) ne sont **pas** transmis aux
  commandes ; stdin fermé (pas de programme interactif), durée et sortie limitées, arbre de
  processus tué au dépassement ou à l'annulation ;
- une approbation est liée à l'appel exact : si le modèle change la commande après votre accord,
  il faut revalider.

**Pas** protégé : ce n'est **pas un bac à sable**. Une commande approuvée (ou tout en mode
autonome) s'exécute avec **vos droits** et peut lire/modifier n'importe quoi que _vous_ pouvez
modifier. Le contenu des fichiers et des pages peut contenir des instructions piégées
(« prompt injection ») : gardez la validation active pour du code que vous ne connaissez pas,
travaillez dans un dépôt Git (pour pouvoir annuler) et ne mettez en dossier autorisé que ce
qui doit l'être.

---

## 6. Limites connues (à lire)

- **Interface testée par compilation, pas dans un navigateur.** Les composants Svelte passent
  `svelte-check` (0 erreur sur les nouveaux fichiers), compilent sans avertissement et la logique
  d'affichage des notifications a ses tests unitaires, mais je n'ai pas pu ouvrir l'interface dans
  un vrai navigateur. Attendez-vous à d'éventuels ajustements visuels.
- **Backend testé en bout en bout sous Linux** (vraie application, vraie base, faux Claude).
  **Windows et macOS non testés** : le code gère `cmd.exe`, `taskkill`, l'encodage OEM et les
  fins de ligne CRLF, mais cela n'a pas été exécuté sur Windows.
- La détection de limite s'appuie sur les **erreurs renvoyées par l'API du fournisseur**
  (429, quota, surcharge). Un message de limite affiché _dans le texte d'une réponse_ (par
  exemple par un agent Claude Code externe) n'est pas détecté.
- Les traductions sont fournies en **français** (et anglais par défaut) ; les autres langues
  affichent l'anglais.
- Les politiques d'approbation de l'agent local sont indépendantes de la fonction globale
  « permissions d'outils » : elles s'appliquent même si celle-ci est désactivée.

---

## 7. Fichiers

**Ajoutés (backend)** : `utils/usage_limit.py` (détection des limites), `utils/chat_queue.py`
(moteur), `models/chat_queue.py` + migration `b7c1e9a4d2f0`, `routers/chat_queue.py`,
`routers/local_agent.py`, `tools/local_agent.py`.
**Modifiés (backend)** : `main.py` (routes, worker, hook d'erreur, `/api/config`), `config.py`,
`utils/middleware.py` (prompt de l'agent, hooks, pause d'approbation tous modes),
`utils/tools.py`, `utils/tool_permissions.py`, `utils/payload.py`.
**Frontend** : `components/queue/*` (page), `chat/LocalAgentButton.svelte`,
`chat/QueueQuickButton.svelte`, `chat/Messages/LimitResumeBanner.svelte`, `apis/chat-queue`,
`apis/local-agent`, `utils/queueEvents.ts`, route `/queue`, entrées de menu, gestion des
événements dans `routes/+layout.svelte`, traductions `fr-FR`.

## 8. Tests

```bash
cd backend/open_webui/test && pip install pytest pytest-asyncio sqlalchemy aiosqlite pydantic
pytest                         # 120 tests : limites, file, moteur, agent local, approbations
npx vitest run src/lib/utils/queueEvents.test.ts       # 18 tests (notifications)
```

Les tests de bout en bout (vrai backend + faux Claude, 80 contrôles) sont dans
`backend/open_webui/test/e2e/`.
