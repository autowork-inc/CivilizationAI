import { Text, TextInput, View, type TextInputProps } from 'react-native';

type Props = TextInputProps & { label: string };

export function TextField({ label, ...inputProps }: Props) {
  return (
    <View className="mb-4">
      <Text className="mb-1.5 text-sm font-medium text-neutral-400">{label}</Text>
      <TextInput
        className="rounded-xl border border-neutral-800 bg-neutral-900 px-4 py-3.5 text-base text-neutral-50"
        placeholderTextColor="#737373"
        selectionColor="#818cf8"
        autoCapitalize="none"
        autoCorrect={false}
        {...inputProps}
      />
    </View>
  );
}
