import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union, cast
from urllib.parse import parse_qs, urlencode

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.paginator import Page
from django.http import HttpRequest, QueryDict
from eyecite.tokenizers import HyperscanTokenizer

from cl.citations.utils import get_citation_depth_between_clusters
from cl.lib.types import CleanData, SearchParam
from cl.lib.utils import (
    cleanup_main_query,
    get_array_of_selected_fields,
    get_child_court_ids_for_parents,
    map_to_docket_entry_sorting,
)
from cl.search.constants import (
    BOOSTS,
    SEARCH_ORAL_ARGUMENT_HL_FIELDS,
    SOLR_OPINION_HL_FIELDS,
    SOLR_PEOPLE_HL_FIELDS,
    SOLR_RECAP_HL_FIELDS,
)
from cl.search.forms import SearchForm
from cl.search.models import (
    SEARCH_TYPES,
    Court,
    OpinionCluster,
    RECAPDocument,
    SearchQuery,
)

HYPERSCAN_TOKENIZER = HyperscanTokenizer(cache_dir=".hyperscan")


def make_get_string(
    request: HttpRequest,
    nuke_fields: Optional[List[str]] = None,
) -> str:
    """Makes a get string from the request object. If necessary, it removes
    the pagination parameters.
    """
    if nuke_fields is None:
        nuke_fields = ["page", "show_alert_modal"]
    get_dict = parse_qs(request.META["QUERY_STRING"])
    for key in nuke_fields:
        try:
            del get_dict[key]
        except KeyError:
            pass
    get_string = urlencode(get_dict, True)
    if len(get_string) > 0:
        get_string += "&"
    return get_string


def merge_form_with_courts(
    courts: Dict,
    search_form: SearchForm,
) -> Tuple[Dict[str, List], str, str]:
    """Merges the courts dict with the values from the search form.

    Final value is like (note that order is significant):
    courts = {
        'federal': [
            {'name': 'Eighth Circuit',
             'id': 'ca8',
             'checked': True,
             'jurisdiction': 'F',
             'has_oral_argument_scraper': True,
            },
            ...
        ],
        'district': [
            {'name': 'D. Delaware',
             'id': 'deld',
             'checked' False,
             'jurisdiction': 'FD',
             'has_oral_argument_scraper': False,
            },
            ...
        ],
        'state': [
            [{}, {}, {}][][]
        ],
        ...
    }

    State courts are a special exception. For layout purposes, they get
    bundled by supreme court and then by hand. Yes, this means new state courts
    requires manual adjustment here.
    """
    # Are any of the checkboxes checked?

    checked_statuses = [
        field.value()
        for field in search_form
        if field.html_name.startswith("court_")
    ]
    no_facets_selected = not any(checked_statuses)
    all_facets_selected = all(checked_statuses)
    court_count = str(
        len([status for status in checked_statuses if status is True])
    )
    court_count_human = court_count
    if all_facets_selected:
        court_count_human = "All"

    for field in search_form:
        if no_facets_selected:
            for court in courts:
                court.checked = True
        else:
            for court in courts:
                # We're merging two lists, so we have to do a nested loop
                # to find the right value.
                if f"court_{court.pk}" == field.html_name:
                    court.checked = field.value()
                    break

    # Build the dict with jurisdiction keys and arrange courts into tabs
    court_tabs: Dict[str, List] = {
        "federal": [],
        "district": [],
        "state": [],
        "special": [],
        "military": [],
        "tribal": [],
    }
    bap_bundle = []
    b_bundle = []
    states = []
    territories = []
    for court in courts:
        if court.jurisdiction == Court.FEDERAL_APPELLATE:
            court_tabs["federal"].append(court)
        elif court.jurisdiction == Court.FEDERAL_DISTRICT:
            court_tabs["district"].append(court)
        elif court.jurisdiction in Court.BANKRUPTCY_JURISDICTIONS:
            # Bankruptcy gets bundled into BAPs and regular courts.
            if court.jurisdiction == Court.FEDERAL_BANKRUPTCY_PANEL:
                bap_bundle.append(court)
            else:
                b_bundle.append(court)
        elif court.jurisdiction in Court.STATE_JURISDICTIONS:
            states.append(court)
        elif court.jurisdiction in Court.TERRITORY_JURISDICTIONS:
            territories.append(court)
        elif court.jurisdiction in [
            Court.FEDERAL_SPECIAL,
            Court.COMMITTEE,
            Court.INTERNATIONAL,
        ]:
            court_tabs["special"].append(court)
        elif court.jurisdiction in Court.MILITARY_JURISDICTIONS:
            court_tabs["military"].append(court)
        elif court.jurisdiction in Court.TRIBAL_JURISDICTIONS:
            court_tabs["tribal"].append(court)

    # Put the bankruptcy bundles in the courts dict
    if bap_bundle:
        court_tabs["bankruptcy_panel"] = [bap_bundle]
    court_tabs["bankruptcy"] = [b_bundle]
    court_tabs["state"] = [states, territories]

    return court_tabs, court_count_human, court_count


def make_fq(
    cd: CleanData,
    field: str,
    key: str,
    make_phrase: bool = False,
    slop: int = 0,
) -> str:
    """Does some minimal processing of the query string to get it into a
    proper field query.

    This is necessary because despite our putting AND as the default join
    method, in some cases Solr decides OR is a better approach. So, to work
    around this bug, we do some minimal query parsing ourselves:

    1. If the user provided a phrase we pass that through.

    1. Otherwise, we insert AND as a conjunction between all words.

    :param cd: The cleaned data dictionary from the form.
    :param field: The Solr field to use for the query (e.g. "caseName")
    :param key: The model form field to use for the query (e.g. "case_name")
    :param make_phrase: Whether we should wrap the query in quotes to make a
    phrase search.
    :param slop: Maximum distance between terms in a phrase for a match.
    Only applicable on make_phrase queries.
    :returns A field query string like "caseName:Roe"
    """
    q = cd[key]
    q = q.replace(":", " ")

    if (q.startswith('"') and q.endswith('"')) and q.count('"') == 2:
        # User used quotes. Just pass it through.
        return f"{field}:({q})"

    if make_phrase:
        # No need to mess with conjunctions. Just wrap in quotes.
        # Include slop for proximity queries, e.g: 1:21-bk-1234 -> 21-1234
        return f'{field}:("{q}"~{slop})'

    # Iterate over the query word by word. If the word is a conjunction
    # word, detect that and use the user's request. Else, make sure there's
    # an AND everywhere there should be.

    # Split the query into words e.g: word1 and phrases e.g: "word2 word3"
    words = re.findall(r'"([^"]*)"|(\S+)', q)
    clean_q = []
    needs_default_conjunction = True
    for group in words:
        word = group[0] if group[0] else group[1]
        if word.lower() in ["and", "or", "not"]:
            clean_q.append(word.upper())
            needs_default_conjunction = False
        else:
            if needs_default_conjunction and clean_q:
                clean_q.append("AND")
            # If word contains at least one " " is a phrase, append "".
            clean_q.append(f'"{word}"' if " " in word else word)
            needs_default_conjunction = True

    fq = f"{field}:({' '.join(clean_q)})"
    return fq


def make_boolean_fq(cd: CleanData, field: str, key: str) -> str:
    return f"{field}:{str(cd[key]).lower()}"


def make_fq_proximity_query(cd: CleanData, field: str, key: str) -> str:
    """Make an fq proximity query, attempting to normalize and user input.

    This neuters the citation query box, but at the same time ensures that a
    query for 22 US 44 doesn't return an item with parallel citations 22 US 88
    and 44 F.2d 92. I.e., this ensures that queries don't span citations. This
    works because internally Solr uses proximity to create multiValue fields.

    See: https://stackoverflow.com/a/33858649/64911 and
         https://github.com/freelawproject/courtlistener/issues/381
    """
    # Remove all valid Solr tokens, replacing with a space.
    q = re.sub(r'[\^\?\*:\(\)!"~\-\[\]]', " ", cd[key])

    # Remove all valid Solr words
    tokens = []
    for token in q.split():
        if token not in ["AND", "OR", "NOT", "TO"]:
            tokens.append(token)
    return f"{field}:(\"{' '.join(tokens)}\"~5)"


def make_date_query(
    query_field: str,
    before: datetime,
    after: datetime,
) -> str:
    """Given the cleaned data from a form, return a valid Solr fq string"""
    if any([before, after]):
        if hasattr(after, "strftime"):
            date_filter = f"[{after.isoformat()}T00:00:00Z TO "
        else:
            date_filter = "[* TO "
        if hasattr(before, "strftime"):
            date_filter = f"{date_filter}{before.isoformat()}T23:59:59Z]"
        else:
            date_filter = f"{date_filter}*]"
    else:
        # No date filters were requested
        return ""
    return f"{query_field}:{date_filter}"


def make_cite_count_query(cd: CleanData) -> str:
    """Given the cleaned data from a form, return a valid Solr fq string"""
    start = cd.get("cited_gt") or "*"
    end = cd.get("cited_lt") or "*"
    if start == "*" and end == "*":
        return ""
    else:
        return f"citeCount:[{start} TO {end}]"


def get_selected_field_string(cd: CleanData, prefix: str) -> str:
    """Pulls the selected checkboxes using the get_array_of_selected_fields
    method, and puts it into Solr strings.

    Final strings are of the form "A" OR "B" OR "C", with quotes in case there
    are spaces in the values.
    """
    selected_fields = [
        f'"{field}"' for field in get_array_of_selected_fields(cd, prefix)
    ]
    if len(selected_fields) == cd[f"_{prefix}count"]:
        # All the boxes are checked. No need for filtering.
        return ""
    else:
        selected_field_string = " OR ".join(selected_fields)
        return selected_field_string


def make_boost_string(fields: Dict[str, float]) -> str:
    qf_array = []
    for k, v in fields.items():
        qf_array.append(f"{k}^{v}")
    return " ".join(qf_array)


def add_boosts(main_params: SearchParam, cd: CleanData) -> None:
    """Add any boosts that make sense for the query."""
    if cd["type"] == SEARCH_TYPES.OPINION and main_params["sort"].startswith(
        "score"
    ):
        main_params["boost"] = "pagerank"

    # Apply standard qf parameters
    qf = BOOSTS["qf"][cd["type"]].copy()
    main_params["qf"] = make_boost_string(qf)

    if cd["type"] in [
        SEARCH_TYPES.OPINION,
        SEARCH_TYPES.RECAP,
        SEARCH_TYPES.DOCKETS,
        SEARCH_TYPES.ORAL_ARGUMENT,
    ]:
        # Give a boost on the case_name field if it's obviously a case_name
        # query.
        vs_query = any(
            [
                " v " in main_params["q"],
                " v. " in main_params["q"],
                " vs. " in main_params["q"],
            ]
        )
        in_re_query = main_params["q"].lower().startswith("in re ")
        matter_of_query = main_params["q"].lower().startswith("matter of ")
        ex_parte_query = main_params["q"].lower().startswith("ex parte ")
        if any([vs_query, in_re_query, matter_of_query, ex_parte_query]):
            qf.update({"caseName": 50})
            main_params["qf"] = make_boost_string(qf)

    # Apply phrase-based boosts
    if cd["type"] in [
        SEARCH_TYPES.OPINION,
        SEARCH_TYPES.RECAP,
        SEARCH_TYPES.DOCKETS,
        SEARCH_TYPES.ORAL_ARGUMENT,
    ]:
        main_params["pf"] = make_boost_string(BOOSTS["pf"][cd["type"]])
        main_params["ps"] = 5


def add_faceting(main_params: SearchParam, cd: CleanData, facet: bool) -> None:
    """Add any faceting filters to the query."""
    if not facet:
        # Faceting is off. Do nothing.
        return

    facet_params = cast(SearchParam, {})
    if cd["type"] == SEARCH_TYPES.OPINION:
        facet_params = {
            "facet": "true",
            "facet.mincount": 0,
            "facet.field": "{!ex=dt}status_exact",
        }
    main_params.update(facet_params)


def add_highlighting(
    main_params: SearchParam,
    cd: CleanData,
    highlight: Union[bool, str],
) -> None:
    """Add any parameters relating to highlighting."""

    if not highlight:
        # highlighting is off, therefore we get the default fl parameter,
        # which gives us all fields. We could set it manually, but there's
        # no need.
        return

    # Common highlighting params up here.
    main_params.update(
        {
            "hl": "true",
            "f.text.hl.snippets": "5",
            "f.text.hl.maxAlternateFieldLength": "500",
            "f.text.hl.alternateField": "text",
        }
    )

    if highlight == "text":
        main_params["hl.fl"] = "text"
        return

    assert highlight == "all", "Got unexpected highlighting value."
    # Requested fields for the main query. We only need the fields
    # here that are not requested as part of highlighting. Facet
    # params are not set here because they do not retrieve results,
    # only counts (they are set to 0 rows).
    if cd["type"] == SEARCH_TYPES.OPINION:
        fl = [
            "absolute_url",
            "citeCount",
            "court_id",
            "dateFiled",
            "download_url",
            "id",
            "cluster_id",
            "local_path",
            "sibling_ids",
            "source",
            "status",
        ]
        hlfl = SOLR_OPINION_HL_FIELDS
    elif cd["type"] in [SEARCH_TYPES.RECAP, SEARCH_TYPES.DOCKETS]:
        fl = [
            "absolute_url",
            "assigned_to_id",
            "attachment_number",
            "attorney",
            "court_id",
            "dateArgued",
            "dateFiled",
            "dateTerminated",
            "docket_absolute_url",
            "docket_id",
            "document_number",
            "id",
            "is_available",
            "page_count",
            "party",
            "referred_to_id",
        ]
        hlfl = SOLR_RECAP_HL_FIELDS
    elif cd["type"] == SEARCH_TYPES.ORAL_ARGUMENT:
        fl = [
            "id",
            "absolute_url",
            "court_id",
            "local_path",
            "source",
            "download_url",
            "docket_id",
            "dateArgued",
            "duration",
        ]
        hlfl = SEARCH_ORAL_ARGUMENT_HL_FIELDS
    elif cd["type"] == SEARCH_TYPES.PEOPLE:
        fl = [
            "id",
            "absolute_url",
            "dob",
            "date_granularity_dob",
            "dod",
            "date_granularity_dod",
            "political_affiliation",
            "aba_rating",
            "school",
            "appointer",
            "supervisor",
            "predecessor",
            "selection_method",
            "court",
        ]
        hlfl = SOLR_PEOPLE_HL_FIELDS
    main_params.update({"fl": ",".join(fl), "hl.fl": ",".join(hlfl)})
    for field in hlfl:
        if field == "text":
            continue
        main_params[f"f.{field}.hl.fragListBuilder"] = "single"  # type: ignore
        main_params[f"f.{field}.hl.alternateField"] = field  # type: ignore


def add_filter_queries(main_params: SearchParam, cd) -> None:
    """Add the fq params"""
    # Changes here are usually mirrored in place_facet_queries, below.
    main_fq = []

    if cd["type"] == SEARCH_TYPES.OPINION:
        if cd["case_name"]:
            main_fq.append(make_fq(cd, "caseName", "case_name"))
        if cd["judge"]:
            main_fq.append(make_fq(cd, "judge", "judge"))
        if cd["docket_number"]:
            main_fq.append(
                make_fq(
                    cd,
                    "docketNumber",
                    "docket_number",
                    make_phrase=True,
                    slop=1,
                )
            )
        if cd["citation"]:
            main_fq.append(make_fq_proximity_query(cd, "citation", "citation"))
        if cd["neutral_cite"]:
            main_fq.append(make_fq(cd, "neutralCite", "neutral_cite"))
        main_fq.append(
            make_date_query("dateFiled", cd["filed_before"], cd["filed_after"])
        )

        # Citation count
        cite_count_query = make_cite_count_query(cd)
        main_fq.append(cite_count_query)

    elif cd["type"] in [SEARCH_TYPES.RECAP, SEARCH_TYPES.DOCKETS]:
        if cd["case_name"]:
            main_fq.append(make_fq(cd, "caseName", "case_name"))
        if cd["description"]:
            main_fq.append(make_fq(cd, "description", "description"))
        if cd["docket_number"]:
            main_fq.append(
                make_fq(
                    cd,
                    "docketNumber",
                    "docket_number",
                    make_phrase=True,
                    slop=1,
                )
            )
        if cd["nature_of_suit"]:
            main_fq.append(make_fq(cd, "suitNature", "nature_of_suit"))
        if cd["cause"]:
            main_fq.append(make_fq(cd, "cause", "cause"))
        if cd["document_number"]:
            main_fq.append(make_fq(cd, "document_number", "document_number"))
        if cd["attachment_number"]:
            main_fq.append(
                make_fq(cd, "attachment_number", "attachment_number")
            )
        if cd["assigned_to"]:
            main_fq.append(make_fq(cd, "assignedTo", "assigned_to"))
        if cd["referred_to"]:
            main_fq.append(make_fq(cd, "referredTo", "referred_to"))
        if cd["available_only"]:
            main_fq.append(
                make_boolean_fq(cd, "is_available", "available_only")
            )
        if cd["party_name"]:
            main_fq.append(make_fq(cd, "party", "party_name"))
        if cd["atty_name"]:
            main_fq.append(make_fq(cd, "attorney", "atty_name"))

        main_fq.append(
            make_date_query("dateFiled", cd["filed_before"], cd["filed_after"])
        )

    elif cd["type"] == SEARCH_TYPES.ORAL_ARGUMENT:
        if cd["case_name"]:
            main_fq.append(make_fq(cd, "caseName", "case_name"))
        if cd["judge"]:
            main_fq.append(make_fq(cd, "judge", "judge"))
        if cd["docket_number"]:
            main_fq.append(make_fq(cd, "docketNumber", "docket_number"))
        main_fq.append(
            make_date_query(
                "dateArgued", cd["argued_before"], cd["argued_after"]
            )
        )

    elif cd["type"] == SEARCH_TYPES.PEOPLE:
        if cd["name"]:
            main_fq.append(make_fq(cd, "name", "name"))
        if cd["dob_city"]:
            main_fq.append(make_fq(cd, "dob_city", "dob_city"))
        if cd["dob_state"]:
            main_fq.append(make_fq(cd, "dob_state_id", "dob_state"))
        if cd["school"]:
            main_fq.append(make_fq(cd, "school", "school"))
        if cd["appointer"]:
            main_fq.append(make_fq(cd, "appointer", "appointer"))
        if cd["selection_method"]:
            main_fq.append(
                make_fq(cd, "selection_method_id", "selection_method")
            )
        if cd["political_affiliation"]:
            main_fq.append(
                make_fq(
                    cd, "political_affiliation_id", "political_affiliation"
                )
            )
        main_fq.append(
            make_date_query("dob", cd["born_before"], cd["born_after"])
        )

    # Facet filters
    if cd["type"] == SEARCH_TYPES.OPINION:
        selected_stats_string = get_selected_field_string(cd, "stat_")
        if len(selected_stats_string) > 0:
            main_fq.append(
                f"{{!tag=dt}}status_exact:({selected_stats_string})"
            )

    selected_courts_string = get_selected_field_string(cd, "court_")
    if len(selected_courts_string) > 0:
        main_fq.append(
            f"court_exact:({get_child_court_ids_for_parents(selected_courts_string)})"
        )

    # If a param has been added to the fq variables, then we add them to the
    # main_params var. Otherwise, we don't, as doing so throws an error.
    if len(main_fq) > 0:
        if "fq" in main_params:
            main_params["fq"].extend(main_fq)
        else:
            main_params["fq"] = main_fq


def add_grouping(main_params: SearchParam, cd: CleanData, group: bool) -> None:
    """Add any grouping parameters."""
    if cd["type"] == SEARCH_TYPES.OPINION:
        # Group clusters. Because this uses faceting, we use the collapse query
        # parser here instead of the usual result grouping. Faceting with
        # grouping has terrible performance.
        group_fq = "{!collapse field=cluster_id sort='type asc'}"
        if "fq" in main_params:
            main_params["fq"].append(group_fq)
        else:
            main_params["fq"] = [group_fq]

    elif (
        cd["type"] in [SEARCH_TYPES.RECAP, SEARCH_TYPES.DOCKETS]
        and group is True
    ):
        docket_query = re.search(r"docket_id:\d+", cd["q"])
        if docket_query:
            group_sort = map_to_docket_entry_sorting(main_params["sort"])
        else:
            group_sort = "score desc"
        if cd["type"] == SEARCH_TYPES.RECAP:
            group_limit = 5 if not docket_query else 500
        elif cd["type"] == SEARCH_TYPES.DOCKETS:
            group_limit = 1 if not docket_query else 500
        group_params = cast(
            SearchParam,
            {
                "group": "true",
                "group.ngroups": "true",
                "group.limit": group_limit,
                "group.field": "docket_id",
                "group.sort": group_sort,
            },
        )
        main_params.update(group_params)


def print_params(params: SearchParam) -> None:
    if settings.DEBUG:
        print(
            "Params sent to search are:\n%s"
            % " &\n".join(f"  {k} = {v}" for k, v in params.items())
        )


def build_main_query(
    cd: CleanData,
    highlight: Union[bool, str] = "all",
    order_by: str = "",
    facet: bool = True,
    group: bool = True,
) -> SearchParam:
    main_params = cast(
        SearchParam,
        {
            "q": cleanup_main_query(cd["q"] or "*"),
            "sort": cd.get("order_by", order_by),
            "caller": "build_main_query",
        },
    )
    add_faceting(main_params, cd, facet)
    add_boosts(main_params, cd)
    add_highlighting(main_params, cd, highlight)
    add_filter_queries(main_params, cd)
    add_grouping(main_params, cd, group)

    print_params(main_params)
    return main_params


def build_main_query_from_query_string(
    query_string,
    updates=None,
    kwargs=None,
) -> Optional[SearchParam]:
    """Build a main query dict from a query string

    :param query_string: A GET string to build from.
    :param updates: A dict that can be added to the normal finished query
    string to override any of its defaults.
    :param kwargs: Kwargs to send to the build_main_query function
    :return: A dict that can be sent to Solr for querying
    """
    qd = QueryDict(query_string)
    search_form = SearchForm(qd)

    if not search_form.is_valid():
        return None

    cd = search_form.cleaned_data
    if kwargs is None:
        main_query = build_main_query(cd)
    else:
        main_query = build_main_query(cd, **kwargs)
    if updates is not None:
        main_query.update(updates)

    return main_query


def build_coverage_query(court: str, q: str, facet_field: str) -> SearchParam:
    """
    Create a coverage that can be used to make a facet query

    :param court: String representation of the court to filter to, e.g. 'ca1',
    defaults to 'all'.
    :param q: A query to limit the coverage query, defaults to '*'
    :param facet_field: The field to do faceting on
    :type facet_field: str
    :return: A coverage query dict
    """
    params = cast(
        SearchParam,
        {
            "facet": "true",
            "facet.range": facet_field,
            "facet.range.start": "1600-01-01T00:00:00Z",  # Assume very early date.
            "facet.range.end": "NOW/DAY",
            "facet.range.gap": "+1YEAR",
            "rows": 0,
            "q": q or "*",  # Without this, results will be omitted.
            "caller": "build_coverage_query",
        },
    )
    if court.lower() != "all":
        params["fq"] = [f"court_exact:{court}"]
    return params


def build_court_count_query(group: bool = False) -> SearchParam:
    """Build a query that returns the count of cases for all courts

    :param group: Should the results be grouped? Note that grouped facets have
    bad performance.
    """
    params = cast(
        SearchParam,
        {
            "q": "*",
            "facet": "true",
            "facet.field": "court_exact",
            "facet.limit": -1,
            "rows": 0,
            "caller": "build_court_count_query",
        },
    )
    if group:
        params.update(
            cast(
                SearchParam,
                {
                    "group": "true",
                    "group.ngroups": "true",
                    "group.field": "docket_id",
                    "group.limit": "0",
                    "group.facet": "true",
                },
            )
        )
    return params


async def add_depth_counts(
    search_data: dict[str, Any],
    search_results: Page,
) -> OpinionCluster | None:
    """If the search data contains a single "cites" term (e.g., "cites:(123)"),
    calculate and append the citation depth information between each Solr/ES
    result and the cited OpinionCluster. We only do this for *single* "cites"
    terms to avoid the complexity of trying to render multiple depth
    relationships for all the possible result-citation combinations.

    :param search_data: The cleaned search form data
    :param search_results: The paginated Solr/ES results
    :return The OpinionCluster if the lookup was successful
    """

    cites_query_matches = re.findall(r"cites:\((\d+)\)", search_data["q"])
    if (
        len(cites_query_matches) == 1
        and search_data["type"] == SEARCH_TYPES.OPINION
    ):
        try:
            cited_cluster = await OpinionCluster.objects.aget(
                sub_opinions__pk=cites_query_matches[0]
            )
        except OpinionCluster.DoesNotExist:
            return None
        else:
            for result in search_results.object_list:
                result["citation_depth"] = (
                    await get_citation_depth_between_clusters(
                        citing_cluster_pk=result["cluster_id"],
                        cited_cluster_pk=cited_cluster.pk,
                    )
                )
            return cited_cluster
    else:
        return None


async def clean_up_recap_document_file(item: RECAPDocument) -> None:
    """Clean up the RecapDocument file-related fields after detecting the file
    doesn't exist in the storage.

    :param item: The RECAPDocument to work on.
    :return: None
    """

    if type(item) == RECAPDocument:
        await sync_to_async(item.filepath_local.delete)()
        item.sha1 = ""
        item.date_upload = None
        item.file_size = None
        item.page_count = None
        item.is_available = False
        await item.asave()


def store_search_query(request: HttpRequest, search_results: dict) -> None:
    """Saves an user's search query in a SearchQuery model

    :param request: the request object
    :param search_results: the dict returned by `do_es_search` function
    :return None
    """
    is_error = search_results.get("error")
    search_query = SearchQuery(
        user=None if request.user.is_anonymous else request.user,
        get_params=request.GET.urlencode(),
        failed=is_error,
        query_time_ms=None,
        hit_cache=False,
        source=SearchQuery.WEBSITE,
        engine=SearchQuery.ELASTICSEARCH,
    )
    if is_error:
        # Leave `query_time_ms` as None if there is an error
        search_query.save()
        return

    search_query.query_time_ms = search_results["results_details"][0]
    # do_es_search returns 1 as query time if the micro cache was hit
    search_query.hit_cache = search_query.query_time_ms == 1

    search_query.save()


def store_search_api_query(
    request: HttpRequest, failed: bool, query_time: int | None, engine: int
) -> None:
    """Store the search query from the Search API.

    :param request: The HTTP request object.
    :param failed: Boolean indicating if the query execution failed.
    :param query_time: The time taken to execute the query in milliseconds or
    None if not applicable.
    :param engine: The search engine used to execute the query.
    :return: None
    """
    SearchQuery.objects.create(
        user=None if request.user.is_anonymous else request.user,
        get_params=request.GET.urlencode(),
        failed=failed,
        query_time_ms=query_time,
        hit_cache=False,
        source=SearchQuery.API,
        engine=engine,
    )
