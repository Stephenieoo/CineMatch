# recom_sys_app/ai_agent.py
"""
AI-powered movie recommendation agent (Groq/phidata).
Contains the agent builder and all user-affinity helper functions
used to build personalised context for the agent prompt.
"""
import re

from .models import UserProfile, Interaction


# ============================================
# User Affinity Helper Functions
# ============================================


def _get_signup_movies(user):
    """
    Read the two movies captured during signup from UserProfile.
    Returns a de-duplicated list of movie titles.
    """
    try:
        row = (
            UserProfile.objects.filter(user=user)
            .values_list("liked_g1_title", "liked_g2_title")
            .first()
        )

        if not row:
            return []

        m1, m2 = row
        titles = [t.strip() for t in (m1, m2) if t and t.strip()]

        seen, out = set(), []
        for t in titles:
            key = re.sub(r"[\W_]+", "", t.lower())
            if key and key not in seen:
                seen.add(key)
                out.append(t)

        return out
    except Exception as e:
        print(f"Error getting signup movies: {e}")
        return []


def _get_signup_genre(user):
    """
    Read the two genres captured during signup from UserProfile.
    Returns a list of genre preferences.
    """
    try:
        row = (
            UserProfile.objects.filter(user=user)
            .values_list("favourite_genre1", "favourite_genre2")
            .first()
        )

        if not row:
            return []

        g1, g2 = row
        return [t.strip() for t in (g1, g2) if t and t.strip()]
    except Exception as e:
        print(f"Error getting signup genres: {e}")
        return []


def _get_user_interactions(user, status=None):
    """
    Get user's movie interactions, optionally filtered by status.
    Returns list of tmdb_ids.
    """
    try:
        interactions = Interaction.objects.filter(user=user)
        if status:
            interactions = interactions.filter(status=status.upper())
        return [i.tmdb_id for i in interactions]
    except Exception as e:
        print(f"Error getting user interactions: {e}")
        return []


def _get_movie_titles_from_ids(tmdb_ids: list, limit: int = 20) -> list:
    """
    Fetch movie titles from TMDB IDs via the TMDB details endpoint.
    Returns a list of movie titles.
    """
    if not tmdb_ids:
        return []

    from .views_movie import _tmdb_details

    titles = []
    for tmdb_id in tmdb_ids[:limit]:
        try:
            det = _tmdb_details(tmdb_id)
            title = det.get("title", "")
            if title:
                titles.append(title)
        except Exception as e:
            print(f"Error fetching title for movie {tmdb_id}: {e}")
            continue

    return titles


# ============================================
# AI Agent Helper Functions
# ============================================


def _as_text(resp):
    """Extract text content from agent response."""
    if isinstance(resp, str):
        return resp

    text = getattr(resp, "content", None)
    if text:
        return text

    msgs = getattr(resp, "messages", None) or []
    for m in reversed(msgs):
        if getattr(m, "role", "") == "assistant" and getattr(m, "content", None):
            return m.content

    return str(resp)


def _extract_titles(agent_text: str) -> list:
    """
    Extract movie titles from agent response.
    Looks for JSON array in the response text.
    """
    import json
    import ast

    matches = list(re.finditer(r"\[[^\]]+\]", agent_text, re.DOTALL))
    if not matches:
        return []

    block = matches[-1].group(0)

    try:
        data = json.loads(block)
        return [s for s in data if isinstance(s, str)][:3]
    except Exception:
        pass

    try:
        data = ast.literal_eval(block)
        return [s for s in data if isinstance(s, str)][:3]
    except Exception:
        return []


def _build_recommendation_agent(user, groq_api_key: str):
    """
    Build and configure the recommendation agent with user preferences.
    Uses UserPreference model for data-driven recommendations.
    """
    from phi.agent import Agent
    from phi.model.groq import Groq
    from django.utils import timezone
    from datetime import timedelta

    from .models import UserPreference
    from .services import PreferenceService

    movies = _get_signup_movies(user)
    genres = _get_signup_genre(user)

    liked_ids = _get_user_interactions(user, status="LIKE")
    disliked_ids = _get_user_interactions(user, status="DISLIKE")
    watch_later_ids = _get_user_interactions(user, status="WATCH_LATER")
    watched_liked_ids = _get_user_interactions(user, status="WATCHED_LIKED")
    watched_disliked_ids = _get_user_interactions(user, status="WATCHED_DISLIKED")

    liked_titles = _get_movie_titles_from_ids(liked_ids, limit=10)
    disliked_titles = _get_movie_titles_from_ids(disliked_ids, limit=10)
    watch_later_titles = _get_movie_titles_from_ids(watch_later_ids, limit=10)
    watched_liked_titles = _get_movie_titles_from_ids(watched_liked_ids, limit=10)
    watched_disliked_titles = _get_movie_titles_from_ids(watched_disliked_ids, limit=10)

    try:
        preference = UserPreference.objects.get(user=user)
        if preference.last_updated < timezone.now() - timedelta(hours=1):
            preference = PreferenceService.update_user_preferences(user)
    except UserPreference.DoesNotExist:
        preference = PreferenceService.update_user_preferences(user)

    preference_context = []

    if preference.genre_preferences:
        top_genres = preference.get_top_genres(limit=5)
        if top_genres:
            genre_details = ", ".join(
                [f"{genre} ({score:.0%} preference)" for genre, score in top_genres]
            )
            preference_context.append(
                f"Based on {preference.total_interactions} interactions, the user's top genre preferences are: {genre_details}."
            )

    if preference.total_interactions > 0:
        preference_context.append(
            f"The user has liked {preference.total_likes} movies and disliked {preference.total_dislikes} movies. "
            f"Average rating given: {preference.average_rating_given:.1f}/10"
            if preference.average_rating_given
            else f"The user has liked {preference.total_likes} movies and disliked {preference.total_dislikes} movies."
        )

    affinity_text = (
        f"The user has affinity to movies like: {', '.join(movies)}"
        if movies
        else "The user has not provided a movie affinity list."
    )
    genre_text = f"The user prefers {' and '.join(genres)} genres." if genres else ""

    liked_text = f"Movies the user has liked: {', '.join(liked_titles)}" if liked_titles else ""
    disliked_text = f"Movies the user has disliked: {', '.join(disliked_titles)}" if disliked_titles else ""
    watch_later_text = f"Movies in user's watch later list: {', '.join(watch_later_titles)}" if watch_later_titles else ""
    watched_liked_text = f"Movies the user has watched and enjoyed: {', '.join(watched_liked_titles)}" if watched_liked_titles else ""
    watched_disliked_text = f"Movies the user has watched and did not enjoy: {', '.join(watched_disliked_titles)}" if watched_disliked_titles else ""

    instructions = [
        "You are an intelligent movie recommendation agent that provides personalized suggestions.",
    ]

    if preference_context:
        instructions.extend(preference_context)
        instructions.append(
            "Use these genre preferences as the PRIMARY guide for recommendations. "
            "Prioritize movies in genres with higher preference scores."
        )

    instructions.append(affinity_text)
    if genre_text:
        instructions.append(genre_text)
    if liked_text:
        instructions.append(liked_text)
    if disliked_text:
        instructions.append(disliked_text)
    if watch_later_text:
        instructions.append(watch_later_text)
    if watched_liked_text:
        instructions.append(watched_liked_text)
    if watched_disliked_text:
        instructions.append(watched_disliked_text)

    instructions.extend(
        [
            "Recommend exactly 3 movies that the user is MOST LIKELY to enjoy based on their preferences.",
            "Prioritize genres with higher preference scores when making recommendations.",
            "Search for movies released after 2020 unless it belongs to one of the classic titles.",
            "Avoid recommending movies the user has already disliked, watched and disliked, or explicitly marked as not interested.",
            "For each movie provide a score of match out of 100% based on:",
            "  1. Genre preference alignment (higher weight for preferred genres)",
            "  2. Similarity to movies the user has enjoyed",
            "  3. Overall quality and reviews",
            "  4. Recency and relevance",
            "Format each as: **Title** — Reason (Match: NN%).",
            "Use markdown to format your answers.",
            'Return the three movies at the end as a JSON array of strings like: ["Movie 1", "Movie 2", "Movie 3"]',
        ]
    )

    agent = Agent(
        name="Recommendation Agent",
        model=Groq(id="openai/gpt-oss-120b", api_key=groq_api_key, temperature=0.9),
        instructions=instructions,
        markdown=True,
    )

    return agent
