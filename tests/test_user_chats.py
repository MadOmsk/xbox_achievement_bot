"""Panel's "Мои чаты" (SPEC 6.2): every chat a person has touched, and
"delete" as a reset back to never-touched-it."""

from __future__ import annotations

from bot.db.repo import Repo

TG_ID = 1
CHAT_A = -100501
CHAT_B = -100502


async def _chat(repo: Repo, chat_id: int, title: str) -> None:
    await repo.upsert_chat(chat_id, title, TG_ID)


async def test_subscribed_chat_is_listed_as_subscribed(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.subscribe(CHAT_A, TG_ID)

    chats = await repo.user_chats(TG_ID)

    assert len(chats) == 1
    assert chats[0].chat_id == CHAT_A
    assert chats[0].is_subscribed is True


async def test_only_seen_chat_is_listed_as_not_subscribed(repo: Repo) -> None:
    """chat_seen alone (never subscribed) still counts as "known" (SPEC 6.3's
    membership definition), just not publishing there."""
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.record_chat_seen(CHAT_A, TG_ID)

    chats = await repo.user_chats(TG_ID)

    assert len(chats) == 1
    assert chats[0].is_subscribed is False


async def test_untouched_chat_does_not_appear(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    # Neither subscribed nor seen — must not show up.
    assert await repo.user_chats(TG_ID) == []


async def test_inactive_chat_is_excluded(repo: Repo) -> None:
    """A chat the bot got kicked from — nothing left to manage there."""
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.subscribe(CHAT_A, TG_ID)
    await repo.set_chat_active(CHAT_A, False)

    assert await repo.user_chats(TG_ID) == []


async def test_unsubscribe_keeps_the_chat_listed(repo: Repo) -> None:
    """Unsubscribing only removes the publishing row — chat_seen (and so the
    chat's place in this list) stays, one tap away from re-subscribing."""
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.subscribe(CHAT_A, TG_ID)
    await repo.record_chat_seen(CHAT_A, TG_ID)

    await repo.unsubscribe(CHAT_A, TG_ID)

    chats = await repo.user_chats(TG_ID)
    assert len(chats) == 1
    assert chats[0].is_subscribed is False


async def test_forget_chat_membership_removes_it_from_the_list(repo: Repo) -> None:
    """"Delete" (SPEC 6.2) clears both subscriptions and chat_seen — the
    chat vanishes from the list entirely, as if never touched."""
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.subscribe(CHAT_A, TG_ID)
    await repo.record_chat_seen(CHAT_A, TG_ID)

    await repo.forget_chat_membership(CHAT_A, TG_ID)

    assert await repo.user_chats(TG_ID) == []
    assert await repo.is_subscribed(CHAT_A, TG_ID) is False


async def test_forget_chat_membership_is_not_a_ban(repo: Repo) -> None:
    """Re-subscribing (or being seen writing again) after "delete" brings the
    chat right back — no third, blocked state (SPEC 6.2: "банов тут нету")."""
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.subscribe(CHAT_A, TG_ID)
    await repo.forget_chat_membership(CHAT_A, TG_ID)

    await repo.subscribe(CHAT_A, TG_ID)

    chats = await repo.user_chats(TG_ID)
    assert len(chats) == 1
    assert chats[0].is_subscribed is True


async def test_new_subscription_defaults_to_all(repo: Repo) -> None:
    """SPEC 9, M-Steam-2e's follow-up: rarity_mode lives per subscription
    now, defaulting to 'all' the same way the old personal setting used to."""
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.subscribe(CHAT_A, TG_ID)

    chats = await repo.user_chats(TG_ID)

    assert chats[0].rarity_mode == "all"


async def test_new_subscription_follows_the_admin_default(repo: Repo) -> None:
    """The starting rarity_mode is admin-configurable now
    (app_settings['default_rarity_mode'], handlers/admin.py), not a value
    baked into the schema — new subscriptions must pick it up."""
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.set_app_setting("default_rarity_mode", "rare")

    await repo.subscribe(CHAT_A, TG_ID)

    chats = await repo.user_chats(TG_ID)
    assert chats[0].rarity_mode == "rare"


async def test_rarity_mode_is_none_while_not_subscribed(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.record_chat_seen(CHAT_A, TG_ID)

    chats = await repo.user_chats(TG_ID)

    assert chats[0].rarity_mode is None


async def test_update_subscription_rarity_mode_is_scoped_to_one_chat(repo: Repo) -> None:
    """The whole point of moving this per chat — the same person can have a
    different answer in each one (SPEC 9, M-Steam-2e's follow-up)."""
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Чат А")
    await _chat(repo, CHAT_B, "Чат Б")
    await repo.subscribe(CHAT_A, TG_ID)
    await repo.subscribe(CHAT_B, TG_ID)

    await repo.update_subscription_rarity_mode(CHAT_A, TG_ID, "rare")

    chats = {c.chat_id: c.rarity_mode for c in await repo.user_chats(TG_ID)}
    assert chats == {CHAT_A: "rare", CHAT_B: "all"}


async def test_publication_targets_carries_the_subscriptions_own_rarity_mode(
    repo: Repo,
) -> None:
    """passes_filters() (services/achievements.py) reads ChatTarget.rarity_mode
    now, not a personal setting — publication_targets() is what feeds it."""
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Гейминг-чат")
    await repo.subscribe(CHAT_A, TG_ID)
    await repo.update_subscription_rarity_mode(CHAT_A, TG_ID, "hidden")

    targets = await repo.publication_targets(TG_ID)

    assert targets[0].rarity_mode == "hidden"


async def test_multiple_chats_are_all_listed(repo: Repo) -> None:
    await repo.ensure_user(TG_ID, "igor")
    await _chat(repo, CHAT_A, "Чат А")
    await _chat(repo, CHAT_B, "Чат Б")
    await repo.subscribe(CHAT_A, TG_ID)
    await repo.record_chat_seen(CHAT_B, TG_ID)

    chats = {c.chat_id: c.is_subscribed for c in await repo.user_chats(TG_ID)}

    assert chats == {CHAT_A: True, CHAT_B: False}
