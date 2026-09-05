# claude-code-session-handoff

<p align="right">
  <a href="../README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.es.md">Español</a> ·
  <a href="README.fa.md">فارسی</a>
</p>

Claude Code compacte automatiquement les sessions longues : il supprime la majeure
partie de votre conversation, la remplace par un résumé et poursuit. On s'en
aperçoit généralement parce que le modèle recommence à demander des choses réglées
deux heures plus tôt.

Ce dépôt prend l'autre chemin. Il mesure à quel point la session est réellement
remplie et, quand la fin est véritablement proche, il exporte la session entière
sur le disque et continue dans une nouvelle qui **lit la transcription du parent
en intégralité** avant de faire quoi que ce soit. Rien n'est perdu dans un résumé,
et la chaîne des sessions reste visible et reprenable.

Il diagnostique aussi un piège de configuration qui mérite d'être connu même si
vous n'installez rien de tout ceci : voir [Le plafonnement](#le-plafonnement).

---

## Installation

Nécessite Python 3.8+ et Claude Code. Rien d'autre ; aucune dépendance à installer.

```bash
git clone https://github.com/IRDcode/claude-code-session-handoff
cd claude-code-session-handoff
python install.py --dry-run     # voir d'abord tous les changements
python install.py
```

Redémarrez ensuite Claude Code et vérifiez ce qu'il a détecté :

```bash
python ~/.claude/skills/long-session-handoff/scripts/session_weight.py --explain
```

L'installation par défaut ne **change pas** la façon dont Claude Code gère le
contexte. La compaction automatique reste telle quelle ; le garde-fou effectue
simplement le relais avant qu'elle puisse se déclencher. Pour tout retirer :

```bash
python install.py --uninstall
```

`settings.json` est sauvegardé avant d'être modifié, vos hooks et votre ligne
d'état existants ne sont pas touchés, et installer deux fois ne fait rien.

## Ce qui est installé

| chemin | de quoi il s'agit |
|---|---|
| `~/.claude/skills/long-session-handoff/` | la procédure que suit le modèle, plus trois scripts |
| `~/.claude/hooks/session-weight-watch.py` | le détecteur, sur quatre événements |
| `~/.claude/hooks/statusline-weight.py` | le poids sur la ligne d'état, à chaque rendu |
| `~/.claude/runtime/` | l'état anti-relance, un journal et un cache de mesure |
| `~/.claude/handoffs/` | les exports, et `chains.json` qui relie parent et enfant |

Quatre événements de hook sont enregistrés : `UserPromptSubmit`, `SessionStart`,
`PreCompact` et `PostCompact`. Vos hooks existants sur ces événements sont
conservés.

## En quoi cela diffère du comportement par défaut

| | Claude Code par défaut | avec ceci installé |
|---|---|---|
| quand la session se remplit | la compaction se déclenche ; l'essentiel de la conversation est jeté et remplacé par un résumé | un relais vous est proposé bien avant ce point |
| ce que sait la session suivante | ce que le résumé a capté, écrit par l'agent qui perdait déjà le fil | la transcription du parent, lue en entier et vérifiée par des décomptes |
| historique écarté | sans référence dans la session vivante | récupéré depuis le disque dans `05-dropped-context.md` |
| à quel point est-ce plein, vraiment ? | `/context` montre le % de la fenêtre | la ligne d'état montre le % du mur qui met réellement fin à la session |
| retrouver la continuation ensuite | dérouler `/resume` | `chains.json` enregistre parent, enfant, le poids au moment de la migration et si la lecture a été vérifiée |

La ligne d'état ressemble à ceci :

```
Opus 5 | ████████░░ 85% 830k/977k | 696t 402tc 6.1h | HANDOFF DUE (5) | no-compact
```

Pourcentage du **mur**, pas de la fenêtre. Ce sont deux choses différentes, parfois
d'un facteur cinq, et c'est tout l'objet de la section suivante.

## Le plafonnement

À lire même si vous n'installez rien.

Claude Code a deux points distincts où une session se termine :

```
la compaction se déclenche à  fenêtre − réserve de réponse (~20k) − tampon de résumé (~13k)
l'envoi est refusé à          plafond − réserve de réponse (~20k) − marge (~3k)
```

Le premier s'applique quand la compaction est active, le second quand elle est
désactivée. Une fenêtre de 200 000 tokens se compacte donc autour de **167 000**.

Voici le piège : **`autoCompactWindow` dans `settings.json` est plafonné au
plafond du modèle, en silence.** Demandez 1 000 000 contre un plafond de 200 000 et
vous obtenez 200 000 — sans que rien dans l'interface ne le signale. Une session
configurée pour un million de tokens se fait compacter à 167 000, trois fois de
suite, pendant que quelque 830 000 tokens déjà payés restent inutilisés.

Ce n'est pas hypothétique. C'est l'origine de ce dépôt : trois compactions avec des
`preTokens` de **167 398 / 167 071 / 166 904**, contre un fichier de configuration
qui indiquait `autoCompactWindow: 1000000`.

`--explain` vous dit dans quel cas vous êtes :

```
WINDOW
  client reported  1,000,000
  ceiling          1,000,000   (source DISABLE_COMPACT+CLAUDE_CODE_MAX_CONTEXT_TOKENS)
  resolved         1,000,000   (source settings)

WALL -- the token count past which no more work happens here
  1,000,000 ceiling - 20,000 reply reserve - 3,000 margin
  = 977,000   then SENDING IS REFUSED (no summary; a handoff is the only exit)
```

S'il affiche `settings CLAMPED to …`, votre réglage de fenêtre est rabaissé.

Une seule configuration échappe au plafonnement : `DISABLE_COMPACT=1` combiné à
`CLAUDE_CODE_MAX_CONTEXT_TOKENS`. L'installateur peut le faire pour vous, mais il
demande d'abord et annonce le coût, car **cela désactive aussi le `/compact`
manuel** :

```bash
python install.py --disable-compact --window 1000000
```

Avec ce réglage, la session ne se termine plus par un résumé : elle se termine par
un refus d'envoi. C'est un vrai compromis. Un refus est surmontable : vous faites
le relais et vous continuez. Un historique détruit en silence ne l'est pas. Mais si
vous ignorez toutes les invites jusqu'au mur, cette session cesse d'accepter des
tours, et il vaut mieux le savoir d'avance. Le garde-fou se déclenche à 85 %,
laissant environ 147 000 tokens de marge, donc en pratique on n'y arrive pas.

Passez ce drapeau si vous préférez garder `/compact`. Le relais fonctionne quand
même.

## Compatibilité

Rien ici ne patche ni n'encapsule Claude Code. L'outil lit deux interfaces
documentées — le contrat stdin/stdout des hooks et la charge de la ligne d'état — et
analyse les transcriptions JSONL que le client écrit déjà. C'est pourquoi il survit
aux mises à jour qui casseraient un outil bâti sur des internes.

Là où un chiffre exact est nécessaire, il vient de la preuve et non d'une
affirmation. Trois couches :

1. La fenêtre vient de `context_window_size`, que le client annonce à son sujet à
   chaque rendu de la ligne d'état.
2. Si la session a déjà été compactée, le déclencheur vient du `preTokens` consigné
   dans la transcription à cet instant : le déclencheur observé, non calculé.
   `score()` le privilégie et l'indique par
   `corrected from observed preTokens`.
3. Ce n'est qu'à défaut des deux qu'il retombe sur l'arithmétique des réserves, et
   `--explain` montre chaque entrée pour que la dérive soit visible plutôt que
   silencieuse.

**Anciennes versions de Claude Code.** Les quatre événements de hook et la ligne
d'état sont stables depuis de nombreuses versions. Si un événement manque chez vous,
ce hook ne se déclenche jamais et le reste fonctionne : le détecteur est additif, pas
un remplacement. `--disable-compact` est la seule partie qui dépend de noms de
réglages précis ; `--explain` vous dira s'il n'a rien changé.

**Systèmes d'exploitation.** Python pur, sans dépendances, sans partie compilée. Les
chemins passent par `os.path`, `CLAUDE_CONFIG_DIR` est respecté partout, et
l'installateur choisit un nom d'interpréteur qui fonctionne dans *votre* shell au
lieu de le figer. Le seul code spécifique à une plateforme force UTF-8 sur stdout, ce
dont Windows a besoin et qui est inoffensif ailleurs.

Vérifiez sur votre propre machine :

```bash
python tests/test_session_weight.py    # arithmétique, la barrière, les deux pièges
python tests/test_compat.py            # plancher de syntaxe, points d'entrée, sortie des hooks
```

`test_compat.py` trouve tous les autres Python installés sur votre machine et
relance la suite sous chacun : une différence de version apparaît comme un échec
plutôt que comme une surprise plus tard.

## Quand cela se déclenche

Sept signaux sont mesurés. Le contexte est le seul à voter sur le **fait** de
bouger ; les autres ne font que préciser **l'urgence**.

| signal | seuil |
|---|---|
| contexte face au mur | ≥ 85 % → relais, ≥ 95 % → ne plus demander et agir |
| tours de l'assistant | ≥ 900 |
| appels d'outils | ≥ 600 |
| temps de travail actif | ≥ 4 h |
| la compaction a déjà eu lieu | au moins une fois |

**Rien n'est proposé en dessous de 62 % du mur, quels que soient les autres
signaux.** Cette barrière existe parce que les autres signaux sont des
*approximations* de la pression de contexte, inventées pour un monde où le contexte
ne pouvait pas être mesuré directement. Mesuré sur la session qui a construit
ceci : 4,3 h de travail plus deux compactions antérieures donnaient « relais
maintenant », alors que le contexte était à 147 527 sur 977 000 — 15 %. Bouger à ce
moment-là aurait jeté 829 473 tokens sans rien gagner.

Deux détails de mesure qui comptent plus qu'il n'y paraît :

- **Le temps actif est la somme des intervalles de moins de 10 minutes**, jamais
  dernier moins premier. Une session laissée ouverte toute la nuit affiche 44 h
  d'amplitude et 11 h de travail ; noter l'amplitude déclenche un relais sur une
  session inactive.
- **Les compactions sont comptées depuis la ligne typée de la transcription**,
  jamais en cherchant une chaîne marqueur. Cherchez le marqueur une fois et il
  apparaît dans votre propre sortie d'outil : le décompte se gonfle tout seul.

L'invite apparaît au plus une fois par palier — 200 tours de plus, ou un dixième de
mur supplémentaire — avec un plancher de 15 minutes. Elle ne se déclenche jamais
dans un sous-agent.

## Le relais lui-même

```
mesurer  →  demander  →  exporter  →  créer la continuation  →  elle lit le parent
```

L'export écrit cinq fichiers : chaque message utilisateur mot pour mot (y compris
ceux envoyés en cours de tour, faciles à perdre), chaque message substantiel de
l'assistant, la transcription complète avec les charges d'outils tronquées, un
index de décomptes, et ce que les compactions précédentes ont écarté.

La continuation est ensuite créée et **réveillée sans interface** pour lire l'export
avant que vous ne l'ouvriez. Elle doit répondre avec des décomptes correspondant à
l'index ; s'ils ne correspondent pas, la lecture était partielle et le relais n'est
pas fait. Cette lecture a lieu dans une session où personne n'attend : la partie
coûteuse d'une migration ne vous coûte donc aucun temps réel.

Vous recevez ensuite l'id et le nom :

```
claude --resume 7157caa1-11ce-4f29-a46a-09913d483fb0
```

ou cherchez le nom dans `/resume` : il reprend les mots du sujet du parent, suivis
de `(cont. 2)`.

## Vérifiez par vous-même

Tout ce qui précède est vérifiable sur votre propre machine. Les scripts affichent
des chiffres, pas des assurances :

```bash
# où ma session se termine-t-elle réellement, et pourquoi ?
session_weight.py --explain

# quel est le poids actuel, avec chaque signal nommé ?
session_weight.py --session-id <uuid>

# lisible par une machine
session_weight.py --session-id <uuid> --json
```

Pour confirmer qu'un changement de configuration a pris effet, ne faites pas
confiance au fichier : lisez la transcription. Trouvez le premier tour dont le total
de tokens dépasse l'ancien seuil et vérifiez qu'aucune nouvelle ligne de compaction
ne le suit.

## Limites

Énoncées franchement, parce qu'un outil qui mesure doit être honnête sur ce qu'il
n'a pas mesuré :

- Les réserves (~20k / ~13k / ~3k) découlent du comportement observé. Une version
  future pourrait les modifier. La défense a trois couches : la fenêtre que le
  client annonce lui-même, le `preTokens` réel consigné dans la transcription (le
  déclencheur observé, que `score()` privilégie), et seulement en dernier cette
  arithmétique de réserves — seule la troisième couche est une supposition.
- Le mur de refus d'envoi a été calculé et corroboré, pas atteint volontairement.
  Le garde-fou est conçu pour que vous n'y arriviez jamais.
- Testé sous Windows avec Python 3.11, 3.12 et 3.14, et vérifié contre la grammaire
  de 3.8. Linux et macOS devraient fonctionner — il ne reste aucun code spécifique à
  une plateforme au-delà de l'encodage de la console — mais ni l'un ni l'autre n'a
  été exécuté de bout en bout.
- L'export en cinq fichiers et le calcul du poids sont couverts par les tests. Le
  réveil sans interface dépend de la possibilité de lancer votre binaire `claude` ;
  si ce n'est pas possible, l'export réussit quand même et l'outil vous dit quoi
  faire.
- Cache de prompt : un relais démarre une nouvelle session, donc son cache part à
  froid. Pour une session proche du mur, c'est un bon échange ; cela reste un coût.

## Contribuer

Les rapports de bug sont bienvenus, en particulier « les chiffres étaient faux chez
moi » : joignez la sortie de `--explain`. Si une version de Claude Code déplace
cette arithmétique, c'est le rapport qui la corrige le plus vite.

Avant d'ouvrir une PR, lancez les deux suites :

```bash
python tests/test_session_weight.py
python tests/test_compat.py
```

## Sécurité

[SECURITY.md](../SECURITY.md) documente précisément ce qui est lu, ce qui est écrit
et ce qui part sur le réseau (rien). Cela vaut une lecture avant d'installer
quelque chose qui touche à vos fichiers de session.

## Licence et crédit

MIT — voir [LICENSE](../LICENSE). Libre d'usage, de modification et de
redistribution, y compris commerciale. La seule condition est que l'avis de
copyright et le texte de la licence voyagent avec le code, de sorte qu'un fork ou
une copie reconditionnée indique toujours son origine.

Si vous utilisez l'approche ou les constats — en particulier le diagnostic de la
fenêtre plafonnée — un lien retour est apprécié. `CITATION.cff` est là pour que le
bouton « Cite this repository » de GitHub produise quelque chose de correct.

Écrit par [IRDkiya](https://github.com/IRDcode).
