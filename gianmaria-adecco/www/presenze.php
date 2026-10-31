<?php
/**
 * Backend della pagina presenze di Adecco BadgeBox (versione PHP di presenze.py).
 *
 * Sul server bastano due file nella stessa cartella: index.html (la pagina) e presenze.php (questo file).
 * La pagina chiama presenze.php?a=<azione>:
 *   GET  presenze.php?a=dati&mese=YYYY-MM    dati del mese letti da BadgeBox
 *   POST presenze.php?a=timbra               {"data","tipo","fasce","nota"}  crea il turno, sostituendo quelli del giorno
 *   POST presenze.php?a=elimina-giorno       {"data"}                        elimina tutti i turni del giorno
 *   POST presenze.php?a=timesheet            {}                              invia il timesheet della settimana corrente (lunedì-domenica, tagliata a fine mese)
 * Le modifiche sono ammesse solo per la settimana corrente (lunedì-domenica); non si può timbrare dopo oggi.
 *
 * Configurazione: compila USERNAME e PASSWORD in CONFIG qui sotto. Il file .php viene eseguito dal web
 * server e non scaricato, quindi le credenziali non sono leggibili dal browser. In alternativa (o in
 * aggiunta) si può usare un file .env con le stesse chiavi, accanto a questo file o all'indirizzo ENV_FILE:
 * se lo usi, tienilo fuori dalla cartella pubblica o bloccane l'accesso dal web server.
 *
 * Requisiti: PHP 7.4+ (mod_php o php-fpm) con l'estensione cURL (pacchetto php-curl) oppure, in
 * mancanza, allow_url_fopen attivo. Variabile d'ambiente opzionale: BADGEBOX_URL.
 *
 * Prova senza web server:  php -S 127.0.0.1:8080 presenze.php   e apri http://127.0.0.1:8080
 */

// --- Configurazione: compila qui ---------------------------------------------------
const CONFIG = [
    'USERNAME' => '',
    'PASSWORD' => '',
    // Facoltativi: orari usati se la richiesta non indica le fasce (la pagina le manda sempre).
    // 'CHECKIN_TIME' => '09:00',
    // 'PAUSE_IN_TIME' => '13:00',
    // 'PAUSE_OUT_TIME' => '14:00',
    // 'CHECKOUT_TIME' => '18:00',
];

const BASE_URL_PREDEFINITA = 'https://adecco.badgebox.com';
const DEVICE_ID = '95a2d39d-0897-6f07-bb78-4fc383e4e44b';

const SESSIONE_TTL = 300;   // secondi in cui si riusa il login; 0 = nuovo login a ogni richiesta
const MAX_CORPO = 10000;    // byte massimi accettati nelle richieste POST

// Tipi di timbratura (campo id_track_voice): -1 normale, 251 smart working, 252 trasferta.
const TIPO_NORMALE = '-1';
const TIPI_VALIDI = ['-1', '251', '252'];
const ETICHETTE_TIPO = ['251' => 'smart working', '252' => 'trasferta'];

// Orari di default (08-13 14-20), sovrascrivibili dal .env.
const ORARI_DEFAULT = [
    'CHECKIN_TIME' => '08:00',
    'PAUSE_IN_TIME' => '13:00',
    'PAUSE_OUT_TIME' => '14:00',
    'CHECKOUT_TIME' => '20:00',
];

if (!ini_get('date.timezone')) {
    date_default_timezone_set('Europe/Rome');
}

/** Errore da mostrare all'utente (nella pagina). */
class Errore extends Exception
{
}

function urlBase()
{
    return rtrim(getenv('BADGEBOX_URL') ?: BASE_URL_PREDEFINITA, '/');
}

function urlApi()
{
    return urlBase() . '/server/version_rc4_0';
}

// --- Parsing di date e orari ---------------------------------------------------

/** YYYY-MM-DD esistente a calendario (31 febbraio escluso). */
function dataValida($s)
{
    if (!preg_match('/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/', $s)) {
        return false;
    }
    list($anno, $mese, $giorno) = array_map('intval', explode('-', $s));
    return checkdate($mese, $giorno, $anno);
}

function fasciaValida($s)
{
    return (bool)preg_match('/^[0-9]{1,2}(:[0-9]{2})?-[0-9]{1,2}(:[0-9]{2})?$/', $s);
}

function tipoValido($s)
{
    return in_array($s, TIPI_VALIDI, true);
}

/** '8' o '08:30' -> '08:00' / '08:30'. null se non valido. */
function normalizzaOra($t)
{
    $parti = explode(':', $t, 2);
    $h = $parti[0];
    $m = isset($parti[1]) ? $parti[1] : '00';
    if (!preg_match('/^[0-9]{1,2}$/', $h) || !preg_match('/^[0-9]{2}$/', $m)) {
        return null;
    }
    if ((int)$h > 23 || (int)$m > 59) {
        return null;
    }
    return sprintf('%02d:%02d', (int)$h, (int)$m);
}

/** '08:30' -> minuti dalla mezzanotte. */
function inMinuti($t)
{
    return (int)substr($t, 0, 2) * 60 + (int)substr($t, 3, 2);
}

/** '08-13' o '08:30-13:00' -> ['08:00', '13:00']. */
function leggiFascia($r)
{
    $parti = explode('-', $r, 2);
    $inizio = normalizzaOra($parti[0]);
    $fine = normalizzaOra(isset($parti[1]) ? $parti[1] : '');
    if ($inizio === null || $fine === null) {
        throw new Errore("Fascia oraria non valida: '$r'.");
    }
    if (inMinuti($inizio) >= inMinuti($fine)) {
        throw new Errore("Fascia oraria non valida: '$r' (la fine deve seguire l'inizio).");
    }
    return [$inizio, $fine];
}

function nuovoTurno($ingresso, $uscita, $pausaDa = '', $pausaA = '')
{
    return ['time_in' => $ingresso, 'time_out' => $uscita, 'pause_in' => $pausaDa, 'pause_out' => $pausaA];
}

/** Una fascia = senza pausa; due fasce = la pausa è lo spazio tra le due. */
function turnoDaFasce(array $fasce)
{
    list($inizio, $fine) = leggiFascia($fasce[0]);
    $turno = nuovoTurno($inizio, $fine);
    if (count($fasce) === 2) {
        list($inizio2, $fine2) = leggiFascia($fasce[1]);
        if (!(inMinuti($turno['time_out']) < inMinuti($inizio2))) {
            throw new Errore('Le due fasce non devono sovrapporsi né toccarsi (serve una pausa).');
        }
        $turno['pause_in'] = $turno['time_out'];
        $turno['pause_out'] = $inizio2;
        $turno['time_out'] = $fine2;
    }
    return $turno;
}

/** Orari di default (08-13 14-20), sovrascrivibili dal .env. */
function turnoDaEnv(array $env)
{
    $orari = [];
    foreach (['CHECKIN_TIME', 'PAUSE_IN_TIME', 'PAUSE_OUT_TIME', 'CHECKOUT_TIME'] as $nome) {
        $valore = isset($env[$nome]) && $env[$nome] !== '' ? $env[$nome] : ORARI_DEFAULT[$nome];
        $orari[$nome] = normalizzaOra($valore);
        if ($orari[$nome] === null) {
            throw new Errore('Errore: orari non validi nel .env (formato HH:MM).');
        }
    }
    $a = inMinuti($orari['CHECKIN_TIME']);
    $b = inMinuti($orari['PAUSE_IN_TIME']);
    $c = inMinuti($orari['PAUSE_OUT_TIME']);
    $d = inMinuti($orari['CHECKOUT_TIME']);
    if (!($a < $b && $b < $c && $c < $d)) {
        throw new Errore('Errore: gli orari nel .env devono essere in ordine (ingresso < pausa < uscita).');
    }
    return nuovoTurno($orari['CHECKIN_TIME'], $orari['CHECKOUT_TIME'], $orari['PAUSE_IN_TIME'], $orari['PAUSE_OUT_TIME']);
}

/** (lunedì, domenica) della settimana di oggi, come stringhe YYYY-MM-DD: i soli giorni modificabili. */
function settimanaCorrente()
{
    $oggi = new DateTimeImmutable('today');
    $lunedi = $oggi->modify('-' . ((int)$oggi->format('N') - 1) . ' days');
    $domenica = $lunedi->modify('+6 days');
    return [$lunedi, $domenica];
}

// --- Configurazione (.env) -----------------------------------------------------

function leggiEnv($percorso)
{
    if (!is_file($percorso)) {
        throw new Errore("file $percorso non trovato.");
    }
    $env = [];
    foreach (preg_split('/\R/', (string)file_get_contents($percorso)) as $riga) {
        $riga = trim($riga);
        if ($riga === '' || $riga[0] === '#') {
            continue;
        }
        if (strpos($riga, 'export ') === 0) {
            $riga = ltrim(substr($riga, 7));
        }
        $pos = strpos($riga, '=');
        if ($pos === false) {
            continue;
        }
        $chiave = trim(substr($riga, 0, $pos));
        $valore = trim(substr($riga, $pos + 1));
        $n = strlen($valore);
        if ($n >= 2 && $valore[0] === $valore[$n - 1] && ($valore[0] === '"' || $valore[0] === "'")) {
            $valore = substr($valore, 1, -1);
        } else {
            $valore = preg_replace('/\s+#.*$/', '', $valore);
        }
        $env[$chiave] = $valore;
    }
    return $env;
}

function leggiCredenziali()
{
    $env = CONFIG;
    $percorso = getenv('ENV_FILE') ?: __DIR__ . '/.env';
    if (is_file($percorso)) {   // il .env è facoltativo e ha la precedenza su CONFIG
        $env = array_merge($env, leggiEnv($percorso));
    }
    foreach (['USERNAME', 'PASSWORD'] as $variabile) {
        if (!isset($env[$variabile]) || $env[$variabile] === '') {
            throw new Errore("credenziali mancanti: compila $variabile nel file .env dell'app (cartella data), oppure in CONFIG all'inizio di presenze.php.");
        }
    }
    return $env;
}

// --- Client BadgeBox -----------------------------------------------------------

/** Messaggio d'errore se la risposta contiene un campo ERROR, altrimenti null. */
function messaggioErrore($risposta)
{
    if (is_array($risposta) && !empty($risposta['ERROR'])) {
        $errore = $risposta['ERROR'];
        if (is_array($errore)) {
            return isset($errore['MESSAGE']) ? (string)$errore['MESSAGE'] : 'errore sconosciuto';
        }
        return (string)$errore;
    }
    return null;
}

/** Legge un valore annidato (es. scava($r, 'user', 'session')); null se manca. */
function scava($oggetto, ...$chiavi)
{
    foreach ($chiavi as $chiave) {
        if (!is_array($oggetto) || !array_key_exists($chiave, $oggetto)) {
            return null;
        }
        $oggetto = $oggetto[$chiave];
    }
    return $oggetto;
}

class BadgeBox
{
    private $username;
    private $password;
    public $session = '';
    public $idEmpl = '';
    public $lang = '';
    public $timezone = '';
    public $csrf = '';

    public function __construct($username, $password)
    {
        $this->username = $username;
        $this->password = $password;
    }

    // -- trasporto --
    private static function http($url, $campi = null, array $intestazioni = [])
    {
        $intestazioni[] = 'User-Agent: Mozilla/5.0';
        $intestazioni[] = 'Expect:';
        $corpo = $campi !== null ? http_build_query($campi, '', '&', PHP_QUERY_RFC3986) : null;

        if (function_exists('curl_init')) {
            $opzioni = [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_FOLLOWLOCATION => true,
                CURLOPT_CONNECTTIMEOUT => 15,
                CURLOPT_TIMEOUT => 60,
                CURLOPT_HTTPHEADER => $intestazioni,
            ];
            if ($corpo !== null) {
                $opzioni[CURLOPT_POST] = true;
                $opzioni[CURLOPT_POSTFIELDS] = $corpo;
            }
            $ch = curl_init($url);
            curl_setopt_array($ch, $opzioni);
            $testo = curl_exec($ch);   // come curl -s: anche con errori HTTP si legge il corpo
            if ($testo === false) {
                $errore = curl_error($ch);
                curl_close($ch);
                throw new Errore("Errore di rete su $url: $errore");
            }
            curl_close($ch);
            return (string)$testo;
        }

        if (ini_get('allow_url_fopen')) {   // senza cURL: stream di PHP
            $http = ['method' => $corpo !== null ? 'POST' : 'GET', 'header' => implode("\r\n", $intestazioni),
                'timeout' => 60, 'ignore_errors' => true, 'follow_location' => 1];
            if ($corpo !== null) {
                $http['content'] = $corpo;
            }
            $testo = @file_get_contents($url, false, stream_context_create(['http' => $http]));
            if ($testo === false) {
                throw new Errore("Errore di rete su $url (estensione cURL non disponibile, provato con gli stream di PHP).");
            }
            return (string)$testo;
        }

        throw new Errore('serve l\'estensione PHP cURL (pacchetto php-curl) oppure allow_url_fopen attivo.');
    }

    private static function json($testo)
    {
        $dati = json_decode($testo, true);
        return json_last_error() === JSON_ERROR_NONE ? $dati : null;
    }

    // -- autenticazione --
    public function login()
    {
        $testo = self::http(urlApi() . '/user/login',
            ['username' => $this->username, 'password' => $this->password],
            ['Content-Type: application/x-www-form-urlencoded; charset=UTF-8']);
        $risposta = self::json($testo);
        $messaggio = messaggioErrore($risposta);
        if ($messaggio !== null) {
            throw new Errore("Login fallito: $messaggio");
        }
        $sessione = scava($risposta, 'user', 'session');
        if (!$sessione) {
            throw new Errore("Errore: sessione non ottenuta. Risposta: $testo");
        }
        $this->session = (string)$sessione;
        $this->idEmpl = (string)scava($risposta, 'user', 'id');
        $this->lang = (string)(scava($risposta, 'user', 'language') ?: 'it');
        $this->timezone = (string)(scava($risposta, 'agency', 'timezone') ?: 'Europe/Rome');
    }

    public function leggiCsrf()
    {
        $testo = self::http(urlBase() . '/app/javascript_1_9/CSFR.min.js.php');
        if (!preg_match('/CSFR_TOKEN="([^"]*)"/', $testo, $m) || $m[1] === '') {
            throw new Errore('Errore: CSFR_TOKEN non ottenuto.');
        }
        $this->csrf = $m[1];
    }

    public function connetti()
    {
        $this->login();
        $this->leggiCsrf();
    }

    // -- chiamate API --
    private function post($endpoint, array $extra, $cookie = true)
    {
        $campi = array_merge(
            ['session' => $this->session, 'id_empl' => $this->idEmpl],
            $extra,
            [
                'lang' => $this->lang,
                'device_id' => DEVICE_ID,
                'device_platform' => 'www',
                'device_name' => 'Web site 43',
                'app_version' => '1.0',
                'csfr_token' => $this->csrf,
                'timezone' => $this->timezone,
            ]
        );
        $intestazioni = [
            'Content-Type: application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With: XMLHttpRequest',
        ];
        if ($cookie) {   // il browser autentica anche via cookie "code"
            $intestazioni[] = 'Accept: application/json, text/javascript, */*; q=0.01';
            $intestazioni[] = 'Cookie: code=' . $this->session;
        }
        $testo = self::http(urlApi() . '/' . $endpoint, $campi, $intestazioni);
        return [$testo, self::json($testo)];
    }

    /** Risposta completa di record/all (records, requests, user...) tra due date YYYY-MM-DD. */
    public function leggiTurni($dal, $al)
    {
        list($testo, $risposta) = $this->post('record/all', [
            'from' => "$dal 00:00:00",
            'to' => "$al 23:59:00",
            'id_contract' => '',
        ]);
        $messaggio = messaggioErrore($risposta);
        if ($messaggio !== null) {
            throw new Errore("Lettura turni fallita: $messaggio");
        }
        if (!is_array(scava($risposta, 'records'))) {
            throw new Errore("Errore: risposta inattesa da record/all: $testo");
        }
        return $risposta;
    }

    /** Turni del giorno YYYY-MM-DD. */
    public function turniDelGiorno($giorno)
    {
        $turni = [];
        foreach ($this->leggiTurni($giorno, $giorno)['records'] as $t) {
            if (is_array($t) && isset($t['day']) && $t['day'] === $giorno) {
                $turni[] = $t;
            }
        }
        return $turni;
    }

    public function eliminaTurno($id)
    {
        if (!preg_match('/^[0-9]+$/', (string)$id)) {
            throw new Errore("Id non valido: '$id' (atteso numerico).");
        }
        list($testo, $risposta) = $this->post('record/delete', ['id' => (string)$id]);
        $messaggio = messaggioErrore($risposta);
        if ($messaggio !== null) {
            throw new Errore("Eliminazione turno $id fallita: $messaggio");
        }
        if (scava($risposta, 'MESSAGE') !== 'ok') {
            throw new Errore("Turno $id: risposta inattesa: $testo");
        }
    }

    /** Crea il turno del giorno YYYY-MM-DD. */
    public function creaTurno($giorno, array $turno, $tipo = TIPO_NORMALE, $nota = '')
    {
        list($anno, $mese, $gg) = explode('-', $giorno);
        $data = "$gg-$mese-$anno";
        $pausaDa = $pausaA = '';
        if ($turno['pause_in'] !== '') {
            $pausaDa = $data . ' ' . $turno['pause_in'];
            $pausaA = $data . ' ' . $turno['pause_out'];
        }
        $campi = [
            'checkbox' => '1',
            'date' => $data,
            'null' => '-1',
            'checkin_d' => $data,
            'fk-checkin_d' => "$gg/$mese/$anno",
            'adc_checkin_h' => $turno['time_in'],
            'checkout_d' => $data,
            'fk-checkout_d' => "$gg/$mese/$anno",
            'adc_checkout_h' => $turno['time_out'],
            'adc_pause_in_h' => $turno['pause_in'],
            'adc_pause_out_h' => $turno['pause_out'],
            'id_contract' => '-1',
            'place' => '56235',
            'id_track_voice' => $tipo,
        ];
        if ($nota !== '') {
            $campi['note'] = $nota;
        }
        $campi += [
            'day' => 'undefined',
            'fields' => '[]',
            'checkin' => $data . ' ' . $turno['time_in'],
            'checkout' => $data . ' ' . $turno['time_out'],
            'pausein' => $pausaDa,
            'pauseout' => $pausaA,
            'group_tags' => '[]',
        ];
        list($testo, $risposta) = $this->post('record/create', $campi, false);
        $messaggio = messaggioErrore($risposta);
        if ($messaggio !== null) {
            throw new Errore("Timbratura del $data fallita: $messaggio");
        }
        return $testo;
    }

    /** Crea il timesheet (timesheet/create) per il periodo dal-al, date YYYY-MM-DD. */
    public function creaTimesheet($dal, $al)
    {
        list($testo, $risposta) = $this->post('timesheet/create', [
            'from_user' => $this->idEmpl,
            'from' => $dal,
            'to' => $al,
            'ref_to' => $al,
            'ref_from' => $dal,
            'id_contract' => '-1',
            'requests' => '',
        ]);
        $messaggio = messaggioErrore($risposta);
        if ($messaggio !== null) {
            throw new Errore("Invio timesheet fallito: $messaggio");
        }
        if (!is_array($risposta)) {
            throw new Errore('Invio timesheet: risposta inattesa: ' . substr(trim($testo), 0, 200));
        }
        return $risposta;
    }

    /** Elimina tutti i turni del giorno. Ritorna quanti ne ha eliminati. */
    public function eliminaGiorno($giorno)
    {
        $turni = $this->turniDelGiorno($giorno);
        foreach ($turni as $t) {
            $this->eliminaTurno($t['id']);
        }
        return count($turni);
    }

    /**
     * Timbratura manuale: elimina i turni già presenti e crea il nuovo. Se non riesce a leggerli
     * o a eliminarli non crea nulla (niente doppioni). Ritorna quanti turni ha sostituito.
     */
    public function sostituisciGiorno($giorno, array $turno, $tipo = TIPO_NORMALE, $nota = '')
    {
        try {
            $presenti = $this->turniDelGiorno($giorno);
        } catch (Errore $e) {
            throw new Errore("Impossibile leggere i turni esistenti del $giorno: non creo nulla. (" . $e->getMessage() . ')');
        }
        foreach ($presenti as $t) {
            try {
                $this->eliminaTurno($t['id']);
            } catch (Errore $e) {
                throw new Errore('Eliminazione dei turni esistenti non riuscita, non creo la nuova timbratura. (' . $e->getMessage() . ')');
            }
        }
        $this->creaTurno($giorno, $turno, $tipo, $nota);
        return count($presenti);
    }

    // -- sessione riusabile tra una richiesta e l'altra (PHP non ha memoria condivisa) --
    private static function fileSessione(array $cred)
    {
        return sys_get_temp_dir() . '/presenze_' . md5(urlBase() . '|' . $cred['USERNAME']) . '.json';
    }

    public static function daSessioneSalvata(array $cred)
    {
        $file = self::fileSessione($cred);
        if (SESSIONE_TTL <= 0 || !is_file($file)) {
            return null;
        }
        $d = json_decode((string)@file_get_contents($file), true);
        if (!is_array($d) || !isset($d['scade']) || $d['scade'] < time()) {
            return null;
        }
        foreach (['session', 'id_empl', 'lang', 'timezone', 'csrf'] as $campo) {
            if (!isset($d[$campo]) || $d[$campo] === '') {
                return null;
            }
        }
        $bb = new BadgeBox($cred['USERNAME'], $cred['PASSWORD']);
        $bb->session = (string)$d['session'];
        $bb->idEmpl = (string)$d['id_empl'];
        $bb->lang = (string)$d['lang'];
        $bb->timezone = (string)$d['timezone'];
        $bb->csrf = (string)$d['csrf'];
        return $bb;
    }

    public function salvaSessione(array $cred)
    {
        if (SESSIONE_TTL <= 0) {
            return;
        }
        $file = self::fileSessione($cred);
        $dati = json_encode([
            'session' => $this->session, 'id_empl' => $this->idEmpl, 'lang' => $this->lang,
            'timezone' => $this->timezone, 'csrf' => $this->csrf, 'scade' => time() + SESSIONE_TTL,
        ]);
        if (@file_put_contents($file, $dati, LOCK_EX) !== false) {
            @chmod($file, 0600);
        }
    }

    public static function dimenticaSessione(array $cred)
    {
        @unlink(self::fileSessione($cred));
    }
}

/**
 * Esegue $operazione($bb) con un client BadgeBox autenticato.
 *
 * Il login (con il token CSRF) si fa una volta ogni SESSIONE_TTL secondi e viene riusato dalle
 * richieste successive. Se con la sessione riusata l'operazione fallisce (per esempio perché è
 * scaduta) si rifà il login e si riprova una sola volta: le operazioni usate qui si possono
 * ripetere senza effetti doppi.
 */
function conClient(array $cred, callable $operazione)
{
    $bb = BadgeBox::daSessioneSalvata($cred);
    if ($bb !== null) {
        try {
            return $operazione($bb);
        } catch (Errore $e) {
            BadgeBox::dimenticaSessione($cred);   // sessione probabilmente scaduta
        }
    }
    $bb = new BadgeBox($cred['USERNAME'], $cred['PASSWORD']);
    $bb->connetti();
    $bb->salvaSessione($cred);
    return $operazione($bb);
}

// --- Dal formato BadgeBox al formato della pagina --------------------------------

/** Legge un campo che può arrivare già decodificato o come JSON in una stringa: ritorna un array. */
function comeArray($valore)
{
    if (is_string($valore) && trim($valore) !== '') {
        $letto = json_decode($valore, true);
        $valore = json_last_error() === JSON_ERROR_NONE ? $letto : null;
    }
    return is_array($valore) ? $valore : [];
}

function eLista(array $a)
{
    return $a === [] || array_keys($a) === range(0, count($a) - 1);
}

/** Toglie le pause (HH:MM) dall'intervallo inizio-fine e ritorna i tratti rimasti. */
function spezzaIntervallo($inizio, $fine, array $pause)
{
    usort($pause, function ($x, $y) {
        return strcmp($x['start'], $y['start']);
    });
    $tratti = [];
    $corrente = $inizio;
    foreach ($pause as $pausa) {
        if (strcmp($pausa['start'], $corrente) > 0) {
            $tratti[] = [$corrente, strcmp($pausa['start'], $fine) < 0 ? $pausa['start'] : $fine];
        }
        if (strcmp($pausa['end'], $corrente) > 0) {
            $corrente = $pausa['end'];
        }
    }
    if (strcmp($corrente, $fine) < 0) {
        $tratti[] = [$corrente, $fine];
    }
    return array_values(array_filter($tratti, function ($t) {
        return strcmp($t[0], $t[1]) < 0;
    }));
}

/**
 * Assenze approvate (state 1, voce di assenza) trasformate in tratti orari per giorno.
 * Ritorna [['day' => '2026-09-16', 'start' => '09:00', 'end' => '13:00', 'label' => 'ROL'], ...].
 */
function assenzeDalMese(array $richieste, $ym)
{
    $assenze = [];
    foreach ($richieste as $r) {
        if (!is_array($r)
            || (string)(isset($r['state']) ? $r['state'] : '') !== '1'
            || (string)(isset($r['voice_absence']) ? $r['voice_absence'] : '') !== '1') {
            continue;
        }
        $inizio = substr((string)(isset($r['h_start']) ? $r['h_start'] : ''), 0, 5);
        $fine = substr((string)(isset($r['h_end']) ? $r['h_end'] : ''), 0, 5);
        if ($inizio === '' || $fine === '' || strcmp($inizio, $fine) >= 0) {
            continue;
        }
        $pause = [];
        foreach (comeArray(isset($r['pauses']) ? $r['pauses'] : null) as $x) {
            if (is_array($x) && !empty($x['start']) && !empty($x['end'])) {
                $pause[] = ['start' => substr((string)$x['start'], 0, 5), 'end' => substr((string)$x['end'], 0, 5)];
            }
        }
        $giorniSchema = comeArray(isset($r['value_schema_days']) ? $r['value_schema_days'] : null);
        $giorni = eLista($giorniSchema) ? [] : array_keys($giorniSchema);
        if (!$giorni) {
            $giorni = [isset($r['start']) ? $r['start'] : null];
        }
        foreach ($giorni as $giorno) {
            if (strpos((string)$giorno, $ym . '-') !== 0) {
                continue;
            }
            foreach (spezzaIntervallo($inizio, $fine, $pause) as $tratto) {
                $assenze[] = [
                    'day' => (string)$giorno,
                    'start' => $tratto[0],
                    'end' => $tratto[1],
                    'label' => (string)(isset($r['voice_name']) ? $r['voice_name'] : ''),
                ];
            }
        }
    }
    return $assenze;
}

/**
 * Legge da BadgeBox turni e assenze del mese. Ritorna [turni, assenze, utente].
 */
function leggiMese($ym, array $cred)
{
    $ultimo = (new DateTimeImmutable("$ym-01"))->format('t');
    $risposta = conClient($cred, function ($bb) use ($ym, $ultimo) {
        return $bb->leggiTurni("$ym-01", "$ym-$ultimo");
    });

    $turni = [];
    foreach ($risposta['records'] as $t) {
        if (!is_array($t)) {
            continue;
        }
        $giorno = isset($t['day']) ? (string)$t['day'] : '';
        $entrata = isset($t['checkin']) ? (string)$t['checkin'] : '';
        $uscita = isset($t['checkout']) ? (string)$t['checkout'] : '';
        if ($giorno === '' || $entrata === '' || $uscita === '' || strpos($giorno, $ym . '-') !== 0) {
            continue;
        }
        $turni[] = [
            'day' => $giorno,
            'checkin' => $entrata,
            'checkout' => $uscita,
            'id_track_voice' => (string)(isset($t['id_track_voice']) ? $t['id_track_voice'] : '-1'),
            'notes' => [
                trim((string)(!empty($t['ckin_note']) ? $t['ckin_note'] : '')),
                trim((string)(!empty($t['ckout_note']) ? $t['ckout_note'] : '')),
            ],
        ];
    }
    usort($turni, function ($a, $b) {
        return strcmp($a['day'] . ' ' . $a['checkin'], $b['day'] . ' ' . $b['checkin']);
    });

    $richieste = isset($risposta['requests']) && is_array($risposta['requests']) ? $risposta['requests'] : [];
    $utente = isset($risposta['user']) && is_array($risposta['user']) ? $risposta['user'] : [];
    return [
        $turni,
        assenzeDalMese($richieste, $ym),
        [
            'name' => isset($utente['name']) ? $utente['name'] : '',
            'surname' => isset($utente['surname']) ? $utente['surname'] : '',
        ],
    ];
}

/**
 * {"2026-10-1": {"punches": [{"time": "09:00 - 13:00", "type": "-1"}], "note": "..."}}
 *
 * type = id_track_voice di BadgeBox (-1 regolare, 251 smart working, 252 trasferta);
 * le assenze (ferie, ROL, permessi...) hanno type "absence". Etichette e colori li decide la pagina.
 */
function costruisciAttendance(array $turni, array $assenze)
{
    $giorni = [];

    $chiaveGiorno = function ($giorno) {
        list($anno, $mese, $g) = array_map('intval', explode('-', $giorno));
        return "$anno-$mese-$g";
    };
    $voce = function ($giorno) use (&$giorni, $chiaveGiorno) {
        $k = $chiaveGiorno($giorno);
        if (!isset($giorni[$k])) {
            $giorni[$k] = ['punti' => [], 'note' => []];
        }
        return $k;
    };

    foreach ($turni as $t) {
        $k = $voce($t['day']);
        $inizio = substr($t['checkin'], 11, 5);
        $giorni[$k]['punti'][] = [$inizio, ['time' => $inizio . ' - ' . substr($t['checkout'], 11, 5), 'type' => $t['id_track_voice']]];
        foreach ($t['notes'] as $nota) {
            if ($nota !== '' && !in_array($nota, $giorni[$k]['note'], true)) {
                $giorni[$k]['note'][] = $nota;
            }
        }
    }

    foreach ($assenze as $a) {
        $k = $voce($a['day']);
        $giorni[$k]['punti'][] = [$a['start'], ['time' => $a['start'] . ' - ' . $a['end'], 'type' => 'absence']];
        $nota = $a['label'] !== '' ? 'Assenza: ' . $a['label'] : '';
        if ($nota !== '' && !in_array($nota, $giorni[$k]['note'], true)) {
            $giorni[$k]['note'][] = $nota;
        }
    }

    $risultato = [];
    foreach ($giorni as $k => $v) {
        $punti = $v['punti'];
        usort($punti, function ($x, $y) {
            return strcmp($x[0], $y[0]);
        });
        $voceGiorno = ['punches' => array_map(function ($p) {
            return $p[1];
        }, $punti)];
        if ($v['note']) {
            $voceGiorno['note'] = implode(' · ', $v['note']);
        }
        $risultato[$k] = $voceGiorno;
    }
    return $risultato;
}

function datiDelMese($ym, array $cred)
{
    list($turni, $assenze, $utente) = leggiMese($ym, $cred);
    $attendance = costruisciAttendance($turni, $assenze);
    return [
        'month' => $ym,
        'generated' => date('d/m/Y H:i'),
        'user' => $utente,
        'attendance' => $attendance ?: new stdClass(),
    ];
}

// --- Scritture (timbra / elimina) ----------------------------------------------

function giornoDellaRichiesta(array $corpo, $ancheFuturo = true)
{
    $giorno = isset($corpo['data']) ? (string)$corpo['data'] : '';
    if (!dataValida($giorno)) {
        throw new Errore('data non valida (atteso YYYY-MM-DD).');
    }
    list($lunedi, $domenica) = settimanaCorrente();
    if ($giorno < $lunedi->format('Y-m-d') || $giorno > $domenica->format('Y-m-d')) {
        throw new Errore(sprintf('modifiche consentite solo nella settimana corrente (%s - %s).',
            $lunedi->format('d/m'), $domenica->format('d/m')));
    }
    if (!$ancheFuturo && $giorno > date('Y-m-d')) {
        throw new Errore('non è possibile anticipare le timbrature: solo fino a oggi.');
    }
    return $giorno;
}

function descriviTimbratura($giorno, array $turno, $etichetta = '', $nota = '')
{
    list($anno, $mese, $gg) = explode('-', $giorno);
    $pausa = $turno['pause_in'] !== '' ? 'pausa ' . $turno['pause_in'] . '-' . $turno['pause_out'] : 'senza pausa';
    $extra = ($etichetta !== '' ? ", $etichetta" : '') . ($nota !== '' ? ", nota \"$nota\"" : '');
    return "Timbratura del $gg-$mese-$anno inviata ({$turno['time_in']}-{$turno['time_out']}, $pausa$extra).";
}

/** Timbratura manuale di un giorno: stesse regole di `timbratura.py [tipo] [DATA] [fasce] [nota]`. */
function timbra(array $corpo, array $cred)
{
    $giorno = giornoDellaRichiesta($corpo, false);
    $tipo = isset($corpo['tipo']) ? (string)$corpo['tipo'] : TIPO_NORMALE;
    if (!tipoValido($tipo)) {
        throw new Errore('tipo non valido (ammessi: -1, 251, 252).');
    }
    $fasce = isset($corpo['fasce']) ? $corpo['fasce'] : [];
    $fasceOk = is_array($fasce) && count($fasce) <= 2;
    if ($fasceOk) {
        foreach ($fasce as $f) {
            if (!is_string($f) || !fasciaValida($f)) {
                $fasceOk = false;
            }
        }
    }
    if (!$fasceOk) {
        throw new Errore('fasce non valide (al massimo due, formato 08-13).');
    }
    $fasce = array_values($fasce);
    $nota = trim(isset($corpo['nota']) ? (string)$corpo['nota'] : '');
    $turno = $fasce ? turnoDaFasce($fasce) : turnoDaEnv($cred);
    $sostituiti = conClient($cred, function ($bb) use ($giorno, $turno, $tipo, $nota) {
        return $bb->sostituisciGiorno($giorno, $turno, $tipo, $nota);
    });
    $etichetta = isset(ETICHETTE_TIPO[$tipo]) ? ETICHETTE_TIPO[$tipo] : '';
    return ['ok' => true, 'sostituiti' => $sostituiti, 'messaggio' => descriviTimbratura($giorno, $turno, $etichetta, $nota)];
}

function eliminaGiornoRichiesta(array $corpo, array $cred)
{
    $giorno = giornoDellaRichiesta($corpo);
    return ['ok' => true, 'eliminati' => conClient($cred, function ($bb) use ($giorno) {
        return $bb->eliminaGiorno($giorno);
    })];
}

/**
 * Periodo del timesheet: di norma lunedì-domenica della settimana di oggi, ma non esce dal mese di oggi.
 * Se il mese finisce a metà settimana (es. giovedì) il periodo va dal lunedì a quel giorno; il giorno
 * dopo, nel mese nuovo, parte dal giorno 1. Così il timesheet va inviato ogni domenica e a fine mese.
 */
function periodoTimesheet()
{
    $oggi = new DateTimeImmutable('today');
    list($lunedi, $domenica) = settimanaCorrente();
    $inizioMese = $oggi->modify('first day of this month');
    $fineMese = $oggi->modify('last day of this month');
    $dal = $lunedi > $inizioMese ? $lunedi : $inizioMese;
    $al = $domenica < $fineMese ? $domenica : $fineMese;
    return [$dal, $al];
}

/** Invia il timesheet del periodo corrente: il periodo lo decide il server, non la richiesta. */
function inviaTimesheet(array $cred)
{
    list($da, $a) = periodoTimesheet();
    $dal = $da->format('Y-m-d');
    $al = $a->format('Y-m-d');
    conClient($cred, function ($bb) use ($dal, $al) {
        return $bb->creaTimesheet($dal, $al);
    });
    return ['ok' => true, 'dal' => $dal, 'al' => $al,
        'messaggio' => 'Timesheet inviato (' . $da->format('d/m') . ' - ' . $a->format('d/m') . ').'];
}

// --- Richieste HTTP --------------------------------------------------------------

function rispondiJson($codice, $dati)
{
    http_response_code($codice);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($dati, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);
    exit;
}

/** Corpo JSON di una richiesta POST (solo application/json, al massimo MAX_CORPO byte). */
function leggiCorpoJson()
{
    if (!isset($_SERVER['REQUEST_METHOD']) || $_SERVER['REQUEST_METHOD'] !== 'POST') {
        rispondiJson(405, ['error' => 'serve una richiesta POST']);
    }
    // Solo JSON: un form di un altro sito non può inviare questo Content-Type senza preflight.
    $tipo = isset($_SERVER['CONTENT_TYPE']) ? $_SERVER['CONTENT_TYPE'] : (isset($_SERVER['HTTP_CONTENT_TYPE']) ? $_SERVER['HTTP_CONTENT_TYPE'] : '');
    if (stripos($tipo, 'application/json') !== 0) {
        rispondiJson(415, ['error' => 'serve Content-Type: application/json']);
    }
    $testo = (string)file_get_contents('php://input', false, null, 0, MAX_CORPO + 1);
    $corpo = $testo === '' || strlen($testo) > MAX_CORPO ? null : json_decode($testo, true);
    if (!is_array($corpo)) {
        rispondiJson(400, ['error' => 'richiesta non valida']);
    }
    return $corpo;
}

/** Azione richiesta: ?a=dati|timbra|elimina-giorno|timesheet oppure il percorso /dati, /timbra, ... */
function azioneRichiesta()
{
    if (isset($_GET['a']) && is_string($_GET['a'])) {
        return $_GET['a'];
    }
    $percorso = parse_url(isset($_SERVER['REQUEST_URI']) ? $_SERVER['REQUEST_URI'] : '/', PHP_URL_PATH);
    $nome = basename(is_string($percorso) ? $percorso : '/');
    return in_array($nome, ['dati', 'timbra', 'elimina-giorno', 'timesheet'], true) ? $nome : '';
}

/** Serve la pagina index.html (o presenze.html), riletta a ogni richiesta. */
function serviPagina()
{
    $percorso = parse_url(isset($_SERVER['REQUEST_URI']) ? $_SERVER['REQUEST_URI'] : '/', PHP_URL_PATH);
    $nome = basename(is_string($percorso) ? $percorso : '/');
    if (!in_array($nome, ['', 'index.html', 'presenze.html', basename(__FILE__)], true)) {
        http_response_code(404);
        header('Content-Type: text/plain; charset=utf-8');
        echo 'Non trovato';
        return;
    }
    foreach (['index.html', 'presenze.html'] as $file) {
        if (is_file(__DIR__ . '/' . $file)) {
            header('Content-Type: text/html; charset=utf-8');
            header('Cache-Control: no-store');
            readfile(__DIR__ . '/' . $file);
            return;
        }
    }
    http_response_code(500);
    header('Content-Type: text/plain; charset=utf-8');
    echo 'File index.html non trovato: mettilo accanto a presenze.php.';
}

function principale()
{
    $azione = azioneRichiesta();
    if ($azione === '') {
        serviPagina();
        return;
    }
    try {
        if ($azione === 'dati') {
            $mese = isset($_GET['mese']) && is_string($_GET['mese']) ? $_GET['mese'] : '';
            if (!preg_match('/^[0-9]{4}-(0[1-9]|1[0-2])$/', $mese)) {
                rispondiJson(400, ['error' => 'mese non valido']);
            }
            rispondiJson(200, datiDelMese($mese, leggiCredenziali()));
        } elseif ($azione === 'timbra' || $azione === 'elimina-giorno' || $azione === 'timesheet') {
            $corpo = leggiCorpoJson();
            $cred = leggiCredenziali();
            if ($azione === 'timbra') {
                rispondiJson(200, timbra($corpo, $cred));
            } elseif ($azione === 'timesheet') {
                rispondiJson(200, inviaTimesheet($cred));
            }
            rispondiJson(200, eliminaGiornoRichiesta($corpo, $cred));
        } else {
            rispondiJson(404, ['error' => 'azione sconosciuta']);
        }
    } catch (Errore $e) {
        rispondiJson($azione === 'dati' ? 502 : 400, ['error' => $e->getMessage()]);
    } catch (Throwable $e) {
        rispondiJson(500, ['error' => 'errore interno: ' . $e->getMessage()]);
    }
}

principale();
