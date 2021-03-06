import asyncio
import httpx
import logging
import re
from collections import Counter
from shapely.geometry import mapping
from idunn import settings
from unidecode import unidecode

logger = logging.getLogger(__name__)

from idunn.api.places_list import ALL_CATEGORIES, MAX_HEIGHT, MAX_WIDTH
from idunn.utils.circuit_breaker import IdunnCircuitBreaker

from .models.geocodejson import Intention
from .bragi_client import bragi_client

DEFAULT_BBOX_WIDTH = 0.02
DEFAULT_BBOX_HEIGHT = 0.01

NLU_POI_TAGS = ["poi", "other"]
NLU_BRAND_TAGS = ["brand"]
NLU_CATEGORY_TAGS = ["cat"]
NLU_PLACE_TAGS = ["city", "country", "state", "street"]

tagger_circuit_breaker = IdunnCircuitBreaker(
    "nlu_tagger_api_breaker",
    int(settings["NLU_BREAKER_MAXFAIL"]),
    int(settings["NLU_BREAKER_TIMEOUT"]),
)

classifier_circuit_breaker = IdunnCircuitBreaker(
    "classifier_tagger_api_breaker",
    int(settings["NLU_BREAKER_MAXFAIL"]),
    int(settings["NLU_BREAKER_TIMEOUT"]),
)


class NLU_Helper:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=0.3, verify=settings["VERIFY_HTTPS"])

    async def post_nlu_classifier(self, text):
        classifier_url = settings["NLU_CLASSIFIER_URL"]
        classifier_domain = settings["NLU_CLASSIFIER_DOMAIN"]
        response_classifier = await self.client.post(
            classifier_url,
            json={"text": text, "domain": classifier_domain, "language": "fr", "count": 1},
        )
        response_classifier.raise_for_status()
        return response_classifier

    async def nlu_classifier(self, text):
        try:
            response_classifier = await classifier_circuit_breaker.call_async(
                self.post_nlu_classifier, text
            )
        except Exception:
            logger.error("Request to NLU classifier failed", exc_info=True)
            return None

        return response_classifier.json()["intention"][0][1]

    def regex_classifier(self, text):
        normalized_text = unidecode(text).lower().strip()
        for category_name, cat in ALL_CATEGORIES.items():
            regex = cat.get("regex")
            if regex and re.search(regex, normalized_text):
                return category_name
        return None

    async def classify_category(self, text):
        if settings["NLU_CLASSIFIER_URL"]:
            return await self.nlu_classifier(text)
        return self.regex_classifier(text)

    @classmethod
    def fuzzy_match(cls, query, response):
        """ Does the response match the query reasonably well ?
        >>> NLU_Helper.fuzzy_match("bastille", "Beuzeville-la-Bastille")
        False
        >>> NLU_Helper.fuzzy_match("paris 20", "Paris 20e Arrondissement")
        True
        >>> NLU_Helper.fuzzy_match("av victor hugo paris", "Avenue Victor Hugo")
        True
        """
        q = unidecode(query.strip()).lower()
        r = unidecode(response).lower()
        if r[: len(q)] == q:
            # Response starts with query
            return True
        if sum((Counter(r) - Counter(q)).values()) < len(q):
            # Number of missing chars to match the response is low
            # compared to the query length
            return True
        return False

    async def build_intention_category_place(
        self, cat_query, place_query, lang, skip_classifier=False
    ):
        async def get_category():
            if skip_classifier:
                return None
            return await self.classify_category(cat_query)

        bragi_result, category_name = await asyncio.gather(
            bragi_client.raw_autocomplete(params={"q": place_query, "lang": lang, "limit": 1}),
            get_category(),
        )

        if not bragi_result["features"]:
            return None

        place = bragi_result["features"][0]
        if not self.fuzzy_match(place_query, place["properties"]["geocoding"]["name"]):
            return None

        bbox = place["properties"]["geocoding"].get("bbox")
        if bbox:
            if bbox[2] - bbox[0] > MAX_WIDTH or bbox[3] - bbox[1] > MAX_HEIGHT:
                return None
        else:
            geometry = place.get("geometry", {})
            if geometry.get("type") == "Point":
                lon, lat = geometry.get("coordinates")
                bbox = [
                    lon - DEFAULT_BBOX_WIDTH / 2,
                    lat - DEFAULT_BBOX_HEIGHT / 2,
                    lon + DEFAULT_BBOX_WIDTH / 2,
                    lat + DEFAULT_BBOX_HEIGHT / 2,
                ]
            else:
                return None

        if category_name:
            return Intention(
                filter={"category": category_name, "bbox": bbox},
                description={"category": category_name, "place": place},
            )
        return Intention(
            filter={"q": cat_query, "bbox": bbox}, description={"query": cat_query, "place": place}
        )

    async def build_intention_category(self, cat_query, lang, skip_classifier=False):
        category_name = None

        if not skip_classifier:
            category_name = await self.classify_category(cat_query)

        if category_name:
            return Intention(
                filter={"category": category_name}, description={"category": category_name},
            )
        return Intention(filter={"q": cat_query}, description={"query": cat_query})

    @classmethod
    def is_poi_request(cls, tags_list):
        """Check if a request is addressed to a POI"""
        return any(t.get("tag") in NLU_POI_TAGS for t in tags_list)

    @classmethod
    def build_brand_query(cls, tags_list):
        tags = [t["phrase"] for t in tags_list if t.get("tag") in NLU_BRAND_TAGS]

        if len(tags) >= 2:
            logger.warning("Ignoring tokenizer for multible brand labels: %s", tags)
            return None

        return next(iter(tags), None)

    @classmethod
    def build_category_query(cls, tags_list):
        tags = [t["phrase"] for t in tags_list if t.get("tag") in NLU_CATEGORY_TAGS]

        if len(tags) >= 2:
            logger.warning("Ignoring tokenizer for multible category labels: %s", tags)
            return None

        return next(iter(tags), None)

    @classmethod
    def build_place_query(cls, tags_list):
        tags = [t["phrase"] for t in tags_list if t.get("tag") in NLU_PLACE_TAGS]

        if not tags:
            return None

        return " ".join(tags)

    async def post_intentions(self, text, lang, focus=None):
        tagger_url = settings["NLU_TAGGER_URL"]
        tagger_domain = settings["NLU_TAGGER_DOMAIN"]
        # this settings is an immutable string required as a parameter for the NLU API
        params = {
            "text": text,
            "lang": lang or settings["DEFAULT_LANGUAGE"],
            "domain": tagger_domain,
        }
        response_nlu = await self.client.post(tagger_url, json=params)
        response_nlu.raise_for_status()
        return response_nlu

    async def get_intentions(self, text, lang, focus=None):
        logs_extra = {
            "intention_detection": {
                "text": text,
                "lang": lang,
                "focus": mapping(focus).get("coordinates") if focus is not None else None,
            }
        }

        try:
            response_nlu = await tagger_circuit_breaker.call_async(
                self.post_intentions, text, lang, focus
            )
        except Exception:
            logger.error("Request to NLU tagger failed", exc_info=True, extra=logs_extra)
            return []

        tags_list = [t for t in response_nlu.json()["NLU"] if t["tag"] != "O"]

        if self.is_poi_request(tags_list):
            logger.info("Detected POI request for '%s'", text, extra=logs_extra)
            return []

        intentions = []
        brand_query = self.build_brand_query(tags_list)
        cat_query = self.build_category_query(tags_list)
        place_query = self.build_place_query(tags_list)
        skip_classifier = brand_query is not None

        logs_extra["intention_detection"].update(
            {
                "brand_query": brand_query,
                "cat_query": cat_query,
                "place_query": place_query,
                "skip_classifier": skip_classifier,
            }
        )

        # If there is a label for both a category and a brand, we consider that the request is
        # ambiguous and ignore both.
        if bool(brand_query) ^ bool(cat_query):
            cat_or_brand_query = brand_query or cat_query

            if place_query:
                # 1 category or brand + 1 place
                # Brands are handled the same way categories except that we don't want to process
                # them with the classifier.
                intention = await self.build_intention_category_place(
                    cat_or_brand_query, place_query, lang=lang, skip_classifier=skip_classifier
                )
                if intention is not None:
                    intentions.append(intention)
            else:
                # 1 category or brand
                intention = await self.build_intention_category(
                    cat_or_brand_query, lang=lang, skip_classifier=skip_classifier
                )
                if intention is not None:
                    # A query tagged as "category" and not recognized by the classifier often
                    # leads to irrelevant intention. Let's ignore them for now.
                    if brand_query or intention.filter.category:
                        intentions.append(intention)

        if intentions:
            intention = intentions[0].dict(exclude_none=True)

            place_props = (
                intention.get("description", {})
                .get("place", {})
                .get("properties", {})
                .get("geocoding", {})
            )

            logs_extra["intention_detection"].update(
                {
                    "res_category": intention.get("filter", {}).get("category"),
                    "res_bbox": intention.get("filter", {}).get("bbox"),
                    "res_query": intention.get("filter", {}).get("q"),
                }
            )

            if place_props:
                logs_extra["intention_detection"].update(
                    {
                        "res_place_id": place_props.get("id"),
                        "res_place_name": place_props.get("name"),
                    }
                )

            logger.info("Detected intentions for '%s'", text, extra=logs_extra)
        else:
            logger.info("No intention detected for '%s'", text, extra=logs_extra)

        return intentions


nlu_client = NLU_Helper()
