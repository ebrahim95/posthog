from posthog.hogql import ast
from posthog.hogql.parser import parse_select, parse_expr
from posthog.hogql.query import execute_hogql_query
from posthog.hogql_queries.web_analytics.ctes import (
    COUNTS_CTE,
    BOUNCE_RATE_CTE,
)
from posthog.hogql_queries.web_analytics.web_analytics_query_runner import (
    WebAnalyticsQueryRunner,
)
from posthog.schema import (
    WebStatsTableQuery,
    WebStatsBreakdown,
    WebStatsTableQueryResponse,
)


class WebStatsTableQueryRunner(WebAnalyticsQueryRunner):
    query: WebStatsTableQuery
    query_type = WebStatsTableQuery

    def to_query(self) -> ast.SelectQuery | ast.SelectUnionQuery:
        with self.timings.measure("bounce_rate_query"):
            bounce_rate_query = parse_select(
                BOUNCE_RATE_CTE,
                timings=self.timings,
                placeholders={
                    "session_where": self.session_where(),
                    "session_having": self.session_having(),
                    "breakdown_by": self.bounce_breakdown(),
                },
                backend="cpp",
            )
        with self.timings.measure("counts_query"):
            counts_query = parse_select(
                COUNTS_CTE,
                timings=self.timings,
                placeholders={
                    "counts_where": self.events_where(),
                    "breakdown_by": self.counts_breakdown(),
                },
                backend="cpp",
            )
        with self.timings.measure("top_pages_query"):
            top_sources_query = parse_select(
                """
SELECT
    counts.breakdown_value as "context.columns.breakdown_value",
    counts.total_pageviews as "context.columns.views",
    counts.unique_visitors as "context.columns.visitors",
    bounce_rate.bounce_rate as "context.columns.bounce_rate"
FROM
    {counts_query} AS counts
LEFT OUTER JOIN
    {bounce_rate_query} AS bounce_rate
ON
    counts.breakdown_value = bounce_rate.breakdown_value
WHERE
    {where_breakdown}
ORDER BY
    "context.columns.views" DESC,
    "context.columns.breakdown_value" DESC
LIMIT 10
                """,
                timings=self.timings,
                placeholders={
                    "counts_query": counts_query,
                    "bounce_rate_query": bounce_rate_query,
                    "where_breakdown": self.where_breakdown(),
                },
                backend="cpp",
            )
        return top_sources_query

    def calculate(self):
        response = execute_hogql_query(
            query_type="top_sources_query",
            query=self.to_query(),
            team=self.team,
            timings=self.timings,
            modifiers=self.modifiers,
        )

        return WebStatsTableQueryResponse(
            columns=response.columns,
            results=response.results,
            timings=response.timings,
            types=response.types,
            hogql=response.hogql,
        )

    def counts_breakdown(self):
        match self.query.breakdownBy:
            case WebStatsBreakdown.Page:
                return ast.Field(chain=["properties", "$pathname"])
            case WebStatsBreakdown.InitialChannelType:
                # use this for now, switch to person.$virt_initial_channel_type when it's working. If fixing or adding
                # anything to this, keep in sync with channel_type.py
                return parse_expr(
                    """
multiIf(
    match(person.properties.$initial_utm_campaign, 'cross-network'),
    'Cross Network',

    (
        match(person.properties.$initial_utm_medium, '^(.*cp.*|ppc|retargeting|paid.*)$') OR
        person.properties.$initial_gclid IS NOT NULL OR
        person.properties.$initial_gad_source IS NOT NULL
    ),
    coalesce(
        hogql_lookupPaidSourceType(person.properties.$initial_utm_source),
        hogql_lookupPaidDomainType(person.properties.$initial_referring_domain),
        if(
            match(person.properties.$initial_utm_campaign, '^(.*(([^a-df-z]|^)shop|shopping).*)$'),
            'Paid Shopping',
            NULL
        ),
        hogql_lookupPaidMediumType(person.properties.$initial_utm_medium),
        multiIf (
            person.properties.$initial_gad_source = '1',
            'Paid Search',

            match(person.properties.$initial_utm_campaign, '^(.*video.*)$'),
            'Paid Video',

            'Paid Other'
        )
    ),

    (
        person.properties.$initial_referring_domain = '$direct'
        AND (person.properties.$initial_utm_medium IS NULL OR person.properties.$initial_utm_medium = '')
        AND (person.properties.$initial_utm_source IS NULL OR person.properties.$initial_utm_source IN ('', '(direct)', 'direct'))
    ),
    'Direct',

    coalesce(
        hogql_lookupOrganicSourceType(person.properties.$initial_utm_source),
        hogql_lookupOrganicDomainType(person.properties.$initial_referring_domain),
        if(
            match(person.properties.$initial_utm_campaign, '^(.*(([^a-df-z]|^)shop|shopping).*)$'),
            'Organic Shopping',
            NULL
        ),
        hogql_lookupOrganicMediumType(person.properties.$initial_utm_medium),
        multiIf(
            match(person.properties.$initial_utm_campaign, '^(.*video.*)$'),
            'Organic Video',

            match(person.properties.$initial_utm_medium, 'push$'),
            'Push',

            NULL
        )
    )
)"""
                )
            case WebStatsBreakdown.InitialPage:
                return ast.Field(chain=["person", "properties", "$initial_pathname"])
            case WebStatsBreakdown.InitialReferringDomain:
                return ast.Field(chain=["person", "properties", "$initial_referring_domain"])
            case WebStatsBreakdown.InitialUTMSource:
                return ast.Field(chain=["person", "properties", "$initial_utm_source"])
            case WebStatsBreakdown.InitialUTMCampaign:
                return ast.Field(chain=["person", "properties", "$initial_utm_campaign"])
            case WebStatsBreakdown.InitialUTMMedium:
                return ast.Field(chain=["person", "properties", "$initial_utm_medium"])
            case WebStatsBreakdown.InitialUTMTerm:
                return ast.Field(chain=["person", "properties", "$initial_utm_term"])
            case WebStatsBreakdown.InitialUTMContent:
                return ast.Field(chain=["person", "properties", "$initial_utm_content"])
            case WebStatsBreakdown.Browser:
                return ast.Field(chain=["properties", "$browser"])
            case WebStatsBreakdown.OS:
                return ast.Field(chain=["properties", "$os"])
            case WebStatsBreakdown.DeviceType:
                return ast.Field(chain=["properties", "$device_type"])
            case WebStatsBreakdown.Country:
                return ast.Field(chain=["properties", "$geoip_country_code"])
            case WebStatsBreakdown.Region:
                return parse_expr(
                    "tuple(properties.$geoip_country_code, properties.$geoip_subdivision_1_code, properties.$geoip_subdivision_1_name)"
                )
            case WebStatsBreakdown.City:
                return parse_expr("tuple(properties.$geoip_country_code, properties.$geoip_city_name)")
            case _:
                raise NotImplementedError("Breakdown not implemented")

    def bounce_breakdown(self):
        match self.query.breakdownBy:
            case WebStatsBreakdown.Page:
                # use initial pathname for bounce rate
                return ast.Call(name="any", args=[ast.Field(chain=["person", "properties", "$initial_pathname"])])
            case WebStatsBreakdown.InitialChannelType:
                # use this for now, switch to person.$virt_initial_channel_type when it's working. If fixing or adding
                # anything to this, keep in sync with channel_type.py
                return parse_expr(
                    """
multiIf(
    match(any(person.properties.$initial_utm_campaign), 'cross-network'),
    'Cross Network',

    (
        match(any(person.properties.$initial_utm_medium), '^(.*cp.*|ppc|retargeting|paid.*)$') OR
        any(person.properties.$initial_gclid) IS NOT NULL OR
        any(person.properties.$initial_gad_source) IS NOT NULL
    ),
    coalesce(
        hogql_lookupPaidSourceType(any(person.properties.$initial_utm_source)),
        hogql_lookupPaidDomainType(any(person.properties.$initial_referring_domain)),
        if(
            match(any(person.properties.$initial_utm_campaign), '^(.*(([^a-df-z]|^)shop|shopping).*)$'),
            'Paid Shopping',
            NULL
        ),
        hogql_lookupPaidMediumType(any(person.properties.$initial_utm_medium)),
        multiIf (
            any(person.properties.$initial_gad_source) = '1',
            'Paid Search',

            match(any(person.properties.$initial_utm_campaign), '^(.*video.*)$'),
            'Paid Video',

            'Paid Other'
        )
    ),

    (
        any(person.properties.$initial_referring_domain) = '$direct'
        AND (any(person.properties.$initial_utm_medium) IS NULL OR any(person.properties.$initial_utm_medium) = '')
        AND (any(person.properties.$initial_utm_source) IS NULL OR any(person.properties.$initial_utm_source) IN ('', '(direct)', 'direct'))
    ),
    'Direct',

    coalesce(
        hogql_lookupOrganicSourceType(any(person.properties.$initial_utm_source)),
        hogql_lookupOrganicDomainType(any(person.properties.$initial_referring_domain)),
        if(
            match(any(person.properties.$initial_utm_campaign), '^(.*(([^a-df-z]|^)shop|shopping).*)$'),
            'Organic Shopping',
            NULL
        ),
        hogql_lookupOrganicMediumType(any(person.properties.$initial_utm_medium)),
        multiIf(
            match(any(person.properties.$initial_utm_campaign), '^(.*video.*)$'),
            'Organic Video',

            match(any(person.properties.$initial_utm_medium), 'push$'),
            'Push',

            NULL
        )
    )
)"""
                )
            case _:
                return ast.Call(name="any", args=[self.counts_breakdown()])

    def where_breakdown(self):
        match self.query.breakdownBy:
            case WebStatsBreakdown.Region:
                return parse_expr('tupleElement("context.columns.breakdown_value", 2) IS NOT NULL')
            case WebStatsBreakdown.City:
                return parse_expr('tupleElement("context.columns.breakdown_value", 2) IS NOT NULL')
            case WebStatsBreakdown.InitialChannelType:
                return parse_expr("TRUE")  # actually show null values
            case WebStatsBreakdown.InitialUTMSource:
                return parse_expr("TRUE")  # actually show null values
            case WebStatsBreakdown.InitialUTMCampaign:
                return parse_expr("TRUE")  # actually show null values
            case WebStatsBreakdown.InitialUTMMedium:
                return parse_expr("TRUE")  # actually show null values
            case WebStatsBreakdown.InitialUTMTerm:
                return parse_expr("TRUE")  # actually show null values
            case WebStatsBreakdown.InitialUTMContent:
                return parse_expr("TRUE")  # actually show null values
            case _:
                return parse_expr('"context.columns.breakdown_value" IS NOT NULL')
