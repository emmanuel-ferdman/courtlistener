from datetime import datetime

from django.conf import settings
from django.template import loader
from django_elasticsearch_dsl import Document, Index, fields

from cl.audio.models import Audio
from cl.lib.search_index_utils import null_map
from cl.lib.utils import deepgetattr
from cl.search.models import Citation, ParentheticalGroup

# Define parenthetical elasticsearch index
parenthetical_group_index = Index("parenthetical_group")
parenthetical_group_index.settings(
    number_of_shards=settings.ELASTICSEARCH_NUMBER_OF_SHARDS,
    number_of_replicas=settings.ELASTICSEARCH_NUMBER_OF_REPLICAS,
)


@parenthetical_group_index.document
class ParentheticalGroupDocument(Document):
    author_id = fields.IntegerField(attr="opinion.author_id")
    caseName = fields.TextField(attr="opinion.cluster.case_name")
    citeCount = fields.IntegerField(attr="opinion.cluster.citation_count")
    citation = fields.ListField(
        fields.KeywordField(),
    )
    cites = fields.ListField(
        fields.IntegerField(),
    )
    cluster_id = fields.IntegerField(attr="opinion.cluster_id")
    court_id = fields.KeywordField(attr="opinion.cluster.docket.court.pk")
    dateArgued = fields.DateField(attr="opinion.cluster.docket.date_argued")
    dateFiled = fields.DateField(attr="opinion.cluster.date_filed")
    dateReargued = fields.DateField(
        attr="opinion.cluster.docket.date_reargued"
    )
    dateReargumentDenied = fields.DateField(
        attr="opinion.cluster.docket.date_reargument_denied"
    )
    describing_opinion_cluster_id = fields.KeywordField(
        attr="representative.describing_opinion.cluster.id"
    )
    describing_opinion_cluster_slug = fields.KeywordField(
        attr="representative.describing_opinion.cluster.slug"
    )
    docket_id = fields.IntegerField(attr="opinion.cluster.docket_id")
    docketNumber = fields.KeywordField(
        attr="opinion.cluster.docket.docket_number"
    )
    joined_by_ids = fields.ListField(
        fields.IntegerField(),
    )
    judge = fields.TextField(
        attr="opinion.cluster.judges",
    )
    lexisCite = fields.ListField(
        fields.KeywordField(),
    )
    neutralCite = fields.ListField(
        fields.KeywordField(),
    )
    opinion_cluster_slug = fields.KeywordField(attr="opinion.cluster.slug")
    opinion_extracted_by_ocr = fields.BooleanField(
        attr="opinion.extracted_by_ocr"
    )
    panel_ids = fields.ListField(
        fields.IntegerField(),
    )
    representative_score = fields.KeywordField(attr="representative.score")
    representative_text = fields.TextField(
        attr="representative.text",
    )
    scdb_id = fields.KeywordField(attr="opinion.cluster.scdb_id")
    status = fields.KeywordField()
    suitNature = fields.TextField(
        attr="opinion.cluster.nature_of_suit",
    )

    class Django:
        model = ParentheticalGroup
        fields = ["score"]

    def prepare_citation(self, instance):
        return [str(cite) for cite in instance.opinion.cluster.citations.all()]

    def prepare_cites(self, instance):
        return [o.pk for o in instance.opinion.opinions_cited.all()]

    def prepare_joined_by_ids(self, instance):
        return [judge.pk for judge in instance.opinion.joined_by.all()]

    def prepare_lexisCite(self, instance):
        try:
            return str(
                instance.opinion.cluster.citations.filter(type=Citation.LEXIS)[
                    0
                ]
            )
        except IndexError:
            pass

    def prepare_neutralCite(self, instance):
        try:
            return str(
                instance.opinion.cluster.citations.filter(
                    type=Citation.NEUTRAL
                )[0]
            )
        except IndexError:
            pass

    def prepare_panel_ids(self, instance):
        return [judge.pk for judge in instance.opinion.cluster.panel.all()]

    def prepare_status(self, instance):
        return instance.opinion.cluster.get_precedential_status_display()


# Define oral arguments elasticsearch index
oral_arguments_index = Index("oral_arguments")
oral_arguments_index.settings(
    number_of_shards=settings.ELASTICSEARCH_NUMBER_OF_SHARDS,
    number_of_replicas=settings.ELASTICSEARCH_NUMBER_OF_REPLICAS,
    analysis=settings.ELASTICSEARCH_DSL["analysis"],
)


@oral_arguments_index.document
class AudioDocument(Document):
    absolute_url = fields.KeywordField(attr="get_absolute_url")
    caseName = fields.TextField(
        attr="case_name",
        analyzer="text_en_splitting_cl",
        fields={
            "exact": fields.TextField(
                attr="case_name", analyzer="english_exact"
            ),
        },
    )
    court = fields.TextField(
        attr="docket.court.full_name",
        analyzer="text_en_splitting_cl",
        fields={
            "exact": fields.TextField(attr="judges", analyzer="english_exact"),
        },
    )
    court_exact = fields.KeywordField(attr="docket.court.pk")
    court_id = fields.KeywordField(attr="docket.court.pk")
    court_citation_string = fields.TextField(
        attr="docket.court.citation_string", analyzer="text_en_splitting_cl"
    )
    docket_id = fields.IntegerField(attr="docket.pk")
    dateArgued = fields.DateField(attr="docket.date_argued")
    dateReargued = fields.DateField(attr="docket.date_reargued")
    dateReargumentDenied = fields.DateField(
        attr="docket.date_reargument_denied"
    )
    docketNumber = fields.TextField(
        attr="docket.docket_number", analyzer="text_en_splitting_cl"
    )
    docket_slug = fields.KeywordField(attr="docket.slug")
    duration = fields.IntegerField(attr="duration")
    download_url = fields.KeywordField(attr="download_url")
    file_size_mp3 = fields.IntegerField()
    id = fields.IntegerField(attr="pk")
    judge = fields.TextField(
        attr="judges",
        analyzer="text_en_splitting_cl",
        fields={
            "exact": fields.TextField(attr="judges", analyzer="english_exact"),
        },
    )
    local_path = fields.KeywordField()
    pacer_case_id = fields.KeywordField(attr="docket.pacer_case_id")
    panel_ids = fields.ListField(
        fields.IntegerField(),
    )
    sha1 = fields.KeywordField(attr="sha1")
    source = fields.KeywordField(attr="source")
    text = fields.TextField(
        analyzer="text_en_splitting_cl",
        fields={
            "exact": fields.TextField(analyzer="english_exact"),
        },
    )
    timestamp = fields.DateField()

    class Django:
        model = Audio

    def prepare_panel_ids(self, instance):
        return [judge.pk for judge in instance.panel.all()]

    def prepare_file_size_mp3(self, instance):
        if instance.local_path_mp3:
            return deepgetattr(instance, "local_path_mp3.size", None)

    def prepare_local_path(self, instance):
        if instance.local_path_mp3:
            return deepgetattr(instance, "local_path_mp3.name", None)

    def prepare_text(self, instance):
        text_template = loader.get_template("indexes/audio_text.txt")
        return text_template.render({"item": instance}).translate(null_map)

    def prepare_timestamp(self, instance):
        return datetime.utcnow()
