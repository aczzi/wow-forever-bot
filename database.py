"""Stockage SQLite et initialisation du catalogue des donjons."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

DEFAULT_CATALOGUE = Path(__file__).resolve().with_name("donjons_catalogue.json")


def load_catalogue(path):
    catalogue = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(catalogue, list) or not catalogue:
        raise ValueError("Le catalogue par défaut doit être une liste non vide de donjons.")
    seen = set()
    for dungeon in catalogue:
        if not isinstance(dungeon, dict):
            raise ValueError("Chaque donjon doit être un objet JSON.")
        key, name = dungeon.get("id"), dungeon.get("name")
        lo, hi = dungeon.get("min_level"), dungeon.get("max_level")
        if (not isinstance(key, str) or not 1 <= len(key) <= 100
                or not isinstance(name, str) or not 1 <= len(name) <= 100
                or type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi):
            raise ValueError("Chaque donjon doit avoir un id, un nom et des niveaux valides.")
        if key in seen:
            raise ValueError(f"Identifiant de donjon dupliqué : {key}")
        seen.add(key)
    return catalogue


class Store:
    def __init__(self, path, *, catalogue_path=DEFAULT_CATALOGUE):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        try:
            self._initialize(catalogue_path)
        except Exception:
            self.db.close()
            raise

    def _initialize(self, catalogue_path):
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS dungeons (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                min_level INTEGER NOT NULL,
                max_level INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS votes (
                guild INTEGER NOT NULL,
                user INTEGER NOT NULL,
                dungeon TEXT NOT NULL REFERENCES dungeons(id),
                name TEXT NOT NULL,
                done INTEGER NOT NULL CHECK(done IN (0, 1)),
                PRIMARY KEY (guild, user, dungeon)
            );
            CREATE TABLE IF NOT EXISTS messages (
                guild INTEGER NOT NULL,
                channel INTEGER NOT NULL,
                message INTEGER NOT NULL,
                PRIMARY KEY (guild, channel)
            );
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        # Le JSON sert uniquement à initialiser un catalogue vide.
        # La transaction évite de laisser un catalogue partiellement rempli.
        with self.db:
            if not self.db.execute("SELECT 1 FROM dungeons LIMIT 1").fetchone():
                catalogue = load_catalogue(catalogue_path)
                self.db.executemany(
                    "INSERT INTO dungeons (id, name, min_level, max_level) "
                    "VALUES (:id, :name, :min_level, :max_level)", catalogue,
                )

    def catalogue(self):
        return {row['id']: dict(row) for row in self.db.execute(
            "SELECT * FROM dungeons ORDER BY min_level, name"
        )}

    def votes(self, guild):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM votes WHERE guild = ? ORDER BY name, user, dungeon", (guild,)
        )]

    def set_selection(self, guild, user, name, scope, selected):
        scope, selected = set(scope), set(selected)
        if not selected <= scope or not scope <= self.catalogue().keys():
            raise ValueError("Sélection de donjons invalide")
        with self.db:
            self.db.execute("UPDATE votes SET name = ? WHERE guild = ? AND user = ?", (name, guild, user))
            self.db.executemany(
                "INSERT INTO votes (guild, user, dungeon, name, done) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild, user, dungeon) DO UPDATE SET name = excluded.name, done = excluded.done",
                [(guild, user, key, name, int(key in selected)) for key in scope],
            )

    def message(self, guild, channel):
        row = self.db.execute("SELECT message FROM messages WHERE guild = ? AND channel = ?", (guild, channel)).fetchone()
        return row['message'] if row else None

    def set_message(self, guild, channel, message):
        with self.db:
            self.db.execute(
                "INSERT INTO messages VALUES (?, ?, ?) ON CONFLICT(guild, channel) DO UPDATE SET message = excluded.message",
                (guild, channel, message),
            )

    def boards(self):
        return [(row['guild'], row['channel']) for row in self.db.execute("SELECT guild, channel FROM messages")]

    def import_progression(self, guild):
        """Attribute une seule fois l'ancienne progression au premier serveur."""
        with self.db:
            row = self.db.execute("SELECT value FROM metadata WHERE key = 'pending_progression'").fetchone()
            if not row:
                return
            for vote in json.loads(row['value']):
                self.db.execute(
                    "INSERT INTO votes VALUES (?, ?, ?, ?, 1) ON CONFLICT(guild, user, dungeon) DO NOTHING",
                    (guild, vote['user'], vote['dungeon'], vote['name']),
                )
            self.db.execute("DELETE FROM metadata WHERE key = 'pending_progression'")
            self.db.execute("INSERT INTO metadata VALUES ('progression_import_guild', ?)", (str(guild),))

    def close(self):
        self.db.close()

