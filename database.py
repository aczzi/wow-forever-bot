from __future__ import annotations

import json
from pathlib import Path
import sqlite3

DEFAULT_CATALOGUE = Path(__file__).resolve().with_name("dungeons.json")
DEFAULT_RAIDS_CATALOGUE = Path(__file__).resolve().with_name("raids.json")


def load_catalogue(path):
    catalogue = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(catalogue, list) or not catalogue:
        raise ValueError("The default catalogue must be a non-empty list.")
    seen = set()
    for item in catalogue:
        if not isinstance(item, dict):
            raise ValueError("Each item must be a JSON object.")
        key, name = item.get("id"), item.get("name")
        lo, hi = item.get("min_level"), item.get("max_level")
        if (not isinstance(key, str) or not 1 <= len(key) <= 100
                or not isinstance(name, str) or not 1 <= len(name) <= 100
                or type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi):
            raise ValueError("Each item must have a valid id, name, and levels.")
        if key in seen:
            raise ValueError(f"Duplicate identifier: {key}")
        seen.add(key)
    return catalogue


class Store:
    def __init__(self, path, *, catalogue_path=DEFAULT_CATALOGUE, raids_catalogue_path=DEFAULT_RAIDS_CATALOGUE):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        try:
            self._initialize(catalogue_path, raids_catalogue_path)
        except Exception:
            self.db.close()
            raise

    def _initialize(self, catalogue_path, raids_catalogue_path):
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
            CREATE TABLE IF NOT EXISTS raids (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                min_level INTEGER NOT NULL,
                max_level INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS raid_votes (
                guild INTEGER NOT NULL,
                user INTEGER NOT NULL,
                raid TEXT NOT NULL REFERENCES raids(id),
                name TEXT NOT NULL,
                done INTEGER NOT NULL CHECK(done IN (0, 1)),
                PRIMARY KEY (guild, user, raid)
            );
            CREATE TABLE IF NOT EXISTS raid_messages (
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
        
        with self.db:
            if not self.db.execute("SELECT 1 FROM dungeons LIMIT 1").fetchone():
                catalogue = load_catalogue(catalogue_path)
                self.db.executemany(
                    "INSERT INTO dungeons (id, name, min_level, max_level) "
                    "VALUES (:id, :name, :min_level, :max_level)", catalogue,
                )
            if not self.db.execute("SELECT 1 FROM raids LIMIT 1").fetchone():
                raids_catalogue = load_catalogue(raids_catalogue_path)
                self.db.executemany(
                    "INSERT INTO raids (id, name, min_level, max_level) "
                    "VALUES (:id, :name, :min_level, :max_level)", raids_catalogue,
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
            raise ValueError("Invalid dungeon selection")
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

    # --- Raids Methods ---
    def raids_catalogue(self):
        return {row['id']: dict(row) for row in self.db.execute(
            "SELECT * FROM raids ORDER BY min_level, name"
        )}

    def raid_votes(self, guild):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM raid_votes WHERE guild = ? ORDER BY name, user, raid", (guild,)
        )]

    def set_raid_selection(self, guild, user, name, scope, selected):
        scope, selected = set(scope), set(selected)
        if not selected <= scope or not scope <= self.raids_catalogue().keys():
            raise ValueError("Invalid raid selection")
        with self.db:
            self.db.execute("UPDATE raid_votes SET name = ? WHERE guild = ? AND user = ?", (name, guild, user))
            self.db.executemany(
                "INSERT INTO raid_votes (guild, user, raid, name, done) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild, user, raid) DO UPDATE SET name = excluded.name, done = excluded.done",
                [(guild, user, key, name, int(key in selected)) for key in scope],
            )

    def raid_message(self, guild, channel):
        row = self.db.execute("SELECT message FROM raid_messages WHERE guild = ? AND channel = ?", (guild, channel)).fetchone()
        return row['message'] if row else None

    def set_raid_message(self, guild, channel, message):
        with self.db:
            self.db.execute(
                "INSERT INTO raid_messages VALUES (?, ?, ?) ON CONFLICT(guild, channel) DO UPDATE SET message = excluded.message",
                (guild, channel, message),
            )

    def raid_boards(self):
        return [(row['guild'], row['channel']) for row in self.db.execute("SELECT guild, channel FROM raid_messages")]

    def import_progression(self, guild):
        """Attributes the old progression to the first server only once."""
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