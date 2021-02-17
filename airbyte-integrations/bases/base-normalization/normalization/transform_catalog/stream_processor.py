"""
MIT License

Copyright (c) 2020 Airbyte

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import os
from typing import Dict, List, Set

from jinja2 import Template
from normalization.destination_type import DestinationType
from normalization.transform_catalog.destination_name_transformer import DestinationNameTransformer
from normalization.transform_catalog.utils import (
    is_airbyte_column,
    is_array,
    is_boolean,
    is_combining_node,
    is_integer,
    is_number,
    is_object,
    is_simple_property,
    is_string,
    jinja_call,
)


class StreamProcessor(object):
    """
    Takes as input an Airbyte Stream as described in the AirbyteCatalog (stored as Json Schema).
    Associated input raw data is expected to be stored in a staging area called "raw_schema".

    This processor transforms such a stream into a final table in the destination schema.
    This is done by generating a DBT pipeline of transformations (multiple SQL models files)
    in the same intermediate schema "raw_schema". The final output is written in "schema".

    If any nested columns are discovered in the stream, new StreamProcessor will be
    spawned for each children substreams.
    """

    def __init__(self, *args, **kwargs):
        if "parent" in kwargs:
            self.initFromParent(
                parent=kwargs.get("parent"),
                child_name=kwargs.get("child_name"),
                json_column_name=kwargs.get("json_column_name"),
                properties=kwargs.get("properties"),
                is_nested_array=kwargs.get("is_nested_array"),
            )
        else:
            self.init(
                stream_name=kwargs.get("stream_name"),
                output_directory=kwargs.get("output_directory"),
                integration_type=kwargs.get("integration_type"),
                raw_schema=kwargs.get("raw_schema"),
                schema=kwargs.get("schema"),
                json_column_name=kwargs.get("json_column_name"),
                properties=kwargs.get("properties"),
            )

    def initFromParent(self, parent, child_name: str, json_column_name: str, properties: Dict, is_nested_array: bool):
        """
        @param parent is the Stream Processor that originally created this instance to handle a nested column from that parent table.

        @param json_column_name is the name of the column in the parent data table containing the json column to transform
        @param properties is the json schema description of this nested stream
        @param is_nested_array is a boolean flag specifying if the child is a nested array that needs to be extracted

        The child stream processor will create a separate table to contain the unnested data.
        """
        self.init(
            stream_name=child_name,
            output_directory=parent.output_directory,
            integration_type=parent.integration_type,
            raw_schema=parent.raw_schema,
            schema=parent.schema,
            json_column_name=json_column_name,
            properties=properties,
        )
        self.parent = parent
        self.is_nested_array = is_nested_array
        self.json_path = parent.json_path + [child_name]

    def init(
        self,
        stream_name: str,
        output_directory: str,
        integration_type: DestinationType,
        raw_schema: str,
        schema: str,
        json_column_name: str,
        properties: Dict,
    ):
        """
        @param stream_name of the stream being processed

        @param output_directory is the path to the directory where this processor should write the resulting SQL files (DBT models)
        @param integration_type is the destination type of warehouse
        @param raw_schema is the name of the staging intermediate schema where to create internal tables/views
        @param schema is the name of the schema where to store the final tables where to store the transformed data

        @param json_column_name is the name of the column in the raw data table containing the json column to transform
        @param properties is the json schema description of this stream
        """
        self.stream_name: str = stream_name
        self.output_directory: str = output_directory
        self.integration_type: DestinationType = integration_type
        self.raw_schema: str = raw_schema
        self.schema: str = schema
        self.json_column_name: str = json_column_name
        self.properties: Dict = properties

        self.name_transformer: DestinationNameTransformer = DestinationNameTransformer(integration_type)
        self.parent = None
        self.is_nested_array = False
        self.json_path: List[str] = [stream_name]

    def process(self, from_table: str, table_context: Set[str]) -> Dict:
        """
        @param from_table refers to the raw source table to use to extract data from
        @param table_context is a global context recording names of tables in all schema
        """
        # Check properties
        if not self.properties:
            print(f"  Ignoring substream '{self.stream_name}' from {self.current_json_path()} because properties list is empty")
            return

        # Transformation Pipeline for this stream
        from_table = self.write_model(table_context, self.generate_json_parsing_model(from_table), is_intermediate=True, suffix="ab1")
        from_table = self.write_model(table_context, self.generate_column_typing_model(from_table), is_intermediate=True, suffix="ab2")
        from_table = self.write_model(table_context, self.generate_id_hashing_model(from_table), is_intermediate=True, suffix="ab3")
        from_table = self.write_model(table_context, self.generate_final_model(from_table), is_intermediate=False)
        return {from_table: self.find_children_streams()}

    def find_children_streams(self) -> List:
        properties = self.properties
        children: List[StreamProcessor] = []
        for field in properties.keys():
            children_properties = None
            if is_airbyte_column(field):
                pass
            elif is_combining_node(properties[field]):
                # TODO: merge properties of all combinations
                pass
            elif "type" not in properties[field] or is_object(properties[field]["type"]):
                children_properties = find_properties_object([], field, properties[field])
                is_nested_array = False
                json_column_name = f"'{field}'"
            elif is_array(properties[field]["type"]) and "items" in properties[field]:
                quoted_field = self.name_transformer.normalize_column_name(field, in_jinja=True)
                children_properties = find_properties_object([], field, properties[field]["items"])
                is_nested_array = True
                json_column_name = f"unnested_column_value({quoted_field})"
            if children_properties:
                for child_key in children_properties:
                    stream_processor = StreamProcessor(
                        parent=self,
                        child_name=field,
                        json_column_name=json_column_name,
                        properties=children_properties[child_key],
                        is_nested_array=is_nested_array,
                    )
                    children.append(stream_processor)
        return children

    def generate_json_parsing_model(self, from_table: str) -> str:
        template = Template(
            """
{{ unnesting_before_query }}
select
  {%- if parent_hash_id %}
    {{ parent_hash_id }},
  {%- endif %}
  {%- for field in fields %}
    {{ field }},
  {%- endfor %}
    _airbyte_emitted_at
from {{ from_table }}
{{ unnesting_after_query }}
{{ sql_table_comment }}
"""
        )
        sql = template.render(
            unnesting_before_query=self.unnesting_before_query(),
            parent_hash_id=self.parent_hash_id(),
            fields=self.extract_json_columns(),
            from_table=from_table,
            unnesting_after_query=self.unnesting_after_query(),
            sql_table_comment=self.sql_table_comment(),
        )
        return sql

    def extract_json_columns(self):
        return [
            StreamProcessor.extract_json_column(field, self.json_column_name, self.properties[field], self.name_transformer)
            for field in self.properties.keys()
            if not is_airbyte_column(field)
        ]

    @staticmethod
    def extract_json_column(property_name: str, json_column_name: str, definition: Dict, name_transformer: DestinationNameTransformer):
        json_path = [property_name]
        json_extract = jinja_call(f"json_extract({json_column_name}, {json_path})")
        if "type" in definition:
            if is_array(definition["type"]):
                json_extract = jinja_call(f"json_extract_array({json_column_name}, {json_path})")
            elif is_object(definition["type"]):
                json_extract = jinja_call(f"json_extract({json_column_name}, {json_path})")
            elif is_simple_property(definition["type"]):
                json_extract = jinja_call(f"json_extract_scalar({json_column_name}, {json_path})")
        column_name = name_transformer.normalize_column_name(property_name)
        return f"{json_extract} as {column_name}"

    def generate_column_typing_model(self, from_table: str) -> str:
        template = Template(
            """
select
  {%- if parent_hash_id %}
    {{ parent_hash_id }},
  {%- endif %}
  {%- for field in fields %}
    {{ field }},
  {%- endfor %}
    _airbyte_emitted_at
from {{ from_table }}
{{ sql_table_comment }}
    """
        )
        sql = template.render(
            parent_hash_id=self.parent_hash_id(),
            fields=self.cast_property_types(),
            from_table=from_table,
            sql_table_comment=self.sql_table_comment(),
        )
        return sql

    def cast_property_types(self):
        return [self.cast_property_type(field) for field in self.properties.keys() if not is_airbyte_column(field)]

    def cast_property_type(self, property_name: str):
        column_name = self.name_transformer.normalize_column_name(property_name)
        definition = self.properties[property_name]
        if "type" not in definition:
            print(f"WARN: Unknown type for column {property_name} at {self.current_json_path()}")
            return column_name
        elif is_array(definition["type"]):
            return self.cast_property_type_as_array(property_name)
        elif is_object(definition["type"]):
            sql_type = self.cast_property_type_as_object(property_name)
        elif is_integer(definition["type"]):
            sql_type = jinja_call("dbt_utils.type_int()")
        elif is_number(definition["type"]):
            sql_type = jinja_call("dbt_utils.type_float()")
        elif is_boolean(definition["type"]):
            jinja_column = self.name_transformer.normalize_column_name(property_name, in_jinja=True)
            cast_operation = jinja_call(f"cast_to_boolean({jinja_column})")
            return f"{cast_operation} as {column_name}"
        elif is_string(definition["type"]):
            sql_type = jinja_call("dbt_utils.type_string()")
        else:
            print(f"WARN: Unknown type {definition['type']} for column {property_name} at {self.current_json_path()}")
            return column_name
        return f"cast({column_name} as {sql_type}) as {column_name}"

    def cast_property_type_as_array(self, property_name: str):
        column_name = self.name_transformer.normalize_column_name(property_name)
        if self.integration_type.value == DestinationType.BIGQUERY.value:
            # TODO build a struct/record type from properties JSON schema
            pass
        return column_name

    def cast_property_type_as_object(self, property_name: str):
        if self.integration_type.value == DestinationType.BIGQUERY.value:
            # TODO build a struct/record type from properties JSON schema
            pass
        return jinja_call("type_json()")

    def generate_id_hashing_model(self, from_table: str) -> str:
        template = Template(
            """
select
    *,
    {{ '{{' }} dbt_utils.surrogate_key([
      {%- if parent_hash_id %}
        '{{ parent_hash_id }}',
      {%- endif %}
      {%- for field in fields %}
        {{ field }},
      {%- endfor %}
    ]) {{ '}}' }} as {{ hash_id }}
from {{ from_table }}
{{ sql_table_comment }}
    """
        )
        sql = template.render(
            parent_hash_id=self.parent_hash_id(),
            fields=self.safe_cast_to_strings(),
            hash_id=self.hash_id(),
            from_table=from_table,
            sql_table_comment=self.sql_table_comment(),
        )
        return sql

    def safe_cast_to_strings(self):
        return [
            StreamProcessor.safe_cast_to_string(field, self.properties[field], self.name_transformer)
            for field in self.properties.keys()
            if not is_airbyte_column(field)
        ]

    @staticmethod
    def safe_cast_to_string(property_name: str, definition: Dict, name_transformer: DestinationNameTransformer):
        column_name = name_transformer.normalize_column_name(property_name, in_jinja=True)
        if "type" not in definition:
            return column_name
        elif is_boolean(definition["type"]):
            return f"boolean_to_string({column_name})"
        elif is_array(definition["type"]):
            return f"array_to_string({column_name})"
        else:
            return column_name

    def generate_final_model(self, from_table: str) -> str:
        template = Template(
            """
select
  {%- if parent_hash_id %}
    {{ parent_hash_id }},
  {%- endif %}
  {%- for field in fields %}
    {{ field }},
  {%- endfor %}
    _airbyte_emitted_at,
    {{ hash_id }}
from {{ from_table }}
{{ sql_table_comment }}
    """
        )
        sql = template.render(
            parent_hash_id=self.parent_hash_id(),
            fields=self.list_fields(),
            hash_id=self.hash_id(),
            from_table=from_table,
            sql_table_comment=self.sql_table_comment(),
        )
        return sql

    def list_fields(self):
        return [self.name_transformer.normalize_column_name(field) for field in self.properties.keys() if not is_airbyte_column(field)]

    def write_model(self, table_context: Set[str], sql: str, is_intermediate: bool, suffix: str = "") -> str:
        if is_intermediate:
            output = os.path.join(self.output_directory, "airbyte_views", self.schema)
        else:
            output = os.path.join(self.output_directory, "airbyte_tables", self.schema)
        schema = self.get_schema(is_intermediate)
        table_name = self.generate_new_table_name(table_context, is_intermediate, suffix)
        file = f"{table_name}.sql"
        json_path = self.current_json_path()
        header = "{{ config(schema='" + schema + "') }}\n"
        output_sql_file(output_dir=output, file=file, json_path=json_path, header=header, sql=sql)
        return ref_table(table_name)

    def generate_new_table_name(self, table_context: Set[str], is_intermediate: bool, suffix: str):
        """
        Generates a new table names that is not registered in the schema yet (based on table_name())
        """
        new_table_name = table_name = self.table_name()
        if not is_intermediate and self.parent is None:
            # Top-level stream has priority on table_names
            if new_table_name in table_context:
                # TODO handle collisions between different schemas (dbt works with only one schema for ref()?)
                # so filenames should always be different for dbt but the final table can be same as long as schemas are different:
                # see alias in dbt: https://docs.getdbt.com/docs/building-a-dbt-project/building-models/using-custom-aliases/
                pass
            pass
        else:
            # TODO handle collisions between intermediate tables and children
            for i in range(1, 1000):
                if suffix:
                    new_table_name = self.name_transformer.normalize_table_name(f"{table_name}_{i}_{suffix}")
                else:
                    new_table_name = self.name_transformer.normalize_table_name(f"{table_name}_{i}")
                if new_table_name not in table_context:
                    break
        self.register_new_table(table_context, new_table_name)
        return new_table_name

    def register_new_table(self, table_context: Set[str], table_name: str):
        if table_name not in table_context:
            table_context.add(table_name)
        else:
            raise KeyError(f"Duplicate table {table_name}")

    def get_schema(self, is_intermediate: bool):
        if is_intermediate:
            return self.raw_schema
        else:
            return self.schema

    def current_json_path(self) -> str:
        return "/".join(self.json_path)

    def table_name(self) -> str:
        return self.name_transformer.normalize_table_name(self.stream_name)

    def sql_table_comment(self) -> str:
        if len(self.json_path) > 1:
            return f"-- {self.table_name()} from {self.current_json_path()}"
        else:
            return f"-- {self.table_name()}"

    def hash_id(self) -> str:
        return self.name_transformer.normalize_column_name(f"_airbyte_{self.table_name()}_hashid")

    # Nested Streams

    def parent_hash_id(self) -> str:
        if self.parent:
            return self.parent.hash_id()
        return ""

    def unnesting_before_query(self):
        if self.parent and self.is_nested_array:
            quoted_name = self.name_transformer.normalize_table_name(self.parent.stream_name)
            quoted_field = self.name_transformer.normalize_column_name(self.stream_name, in_jinja=True)
            return jinja_call(f"unnest_cte({quoted_name}, {quoted_field})")
        return ""

    def unnesting_after_query(self):
        result = ""
        if self.parent:
            cross_join = ""
            if self.is_nested_array:
                quoted_name = f"'{self.name_transformer.normalize_table_name(self.parent.stream_name)}'"
                quoted_field = self.name_transformer.normalize_column_name(self.stream_name, in_jinja=True)
                cross_join = jinja_call(f"cross_join_unnest({quoted_name}, {quoted_field})")
            column_name = self.name_transformer.normalize_column_name(self.stream_name)
            result = f"""
{cross_join}
where {column_name} is not null"""
        return result


# Static Functions


def ref_table(table_name) -> str:
    return "{{ ref('" + table_name + "') }}"


def output_sql_file(output_dir: str, file: str, json_path: str, header: str, sql: str):
    """
    @param output_dir is the path to the file to be written
    @param file is the filename to be written
    @param json_path is the json path in the catalog where this stream is originated from
    @param header is the dbt header to be written in the generated model file
    @param sql is the dbt sql content to be written in the generated model file
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    print(f"  Generating {file} from {json_path}")
    with open(os.path.join(output_dir, file), "w") as f:
        f.write(header)
        for line in sql.splitlines():
            if line.strip():
                f.write(line + "\n")
        f.write("\n")


def find_properties_object(path: List[str], field: str, properties: Dict) -> Dict:
    result = {}
    current_path = path + [field]
    current = "_".join(current_path)
    if isinstance(properties, str) or isinstance(properties, int):
        return {}
    else:
        if "items" in properties:
            return find_properties_object(path, field, properties["items"])
        elif "properties" in properties:
            # we found a properties object
            return {current: properties["properties"]}
        elif "type" in properties and is_simple_property(properties["type"]):
            # we found a basic type
            return {current: None}
        elif isinstance(properties, dict):
            for key in properties.keys():
                child = find_properties_object(path=current_path, field=key, properties=properties[key])
                if child:
                    result.update(child)
        elif isinstance(properties, list):
            for item in properties:
                child = find_properties_object(path=current_path, field=field, properties=item)
                if child:
                    result.update(child)
    return result
