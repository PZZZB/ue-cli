#include "CliAnythingBridgeLibrary.h"

#include "Materials/Material.h"
#if ENGINE_MAJOR_VERSION >= 5
#include "Materials/MaterialAttributeDefinitionMap.h"
#endif
#include "Materials/MaterialExpression.h"
#include "Materials/MaterialExpressionCustom.h"
#include "Materials/MaterialExpressionSetMaterialAttributes.h"
#include "Materials/MaterialExpressionTextureBase.h"
#include "Materials/MaterialFunctionInterface.h"
#include "Materials/MaterialInstance.h"
#include "Materials/MaterialInstanceConstant.h"
#include "Materials/MaterialInterface.h"
#include "MaterialEditingLibrary.h"
#include "Engine/Engine.h"
#include "Engine/Texture2D.h"
#include "Engine/Texture.h"
#include "MaterialShared.h"
#include "MaterialShaderType.h"
#include "RHI.h"
#include "ShaderCompiler.h"
#include "ShaderCompilerCore.h"
#include "Editor.h"
#if ENGINE_MAJOR_VERSION >= 5
#include "DataDrivenShaderPlatformInfo.h"
#endif

#include "LevelEditor.h"
#include "SLevelViewport.h"
#include "Modules/ModuleManager.h"
#include "Framework/Application/SlateApplication.h"
#include "HAL/CriticalSection.h"
#include "HAL/FileManager.h"
#include "HAL/IConsoleManager.h"
#include "ImageUtils.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "RenderingThread.h"
#include "Runtime/Launch/Resources/Version.h"
#include "Slate/SceneViewport.h"

#include "GameFramework/Actor.h"
#include "Components/ActorComponent.h"
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#if ENGINE_MAJOR_VERSION >= 5
#include "StaticMeshComponentLODInfo.h"
#endif
#include "Rendering/ColorVertexBuffer.h"
#include "Blueprint/WidgetTree.h"
#include "Components/CanvasPanel.h"
#include "Components/CanvasPanelSlot.h"
#include "Components/PanelWidget.h"
#include "Components/TextBlock.h"
#include "Components/Image.h"
#include "Components/Widget.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "WidgetBlueprint.h"

#include "UObject/UnrealType.h"
#include "UObject/UObjectIterator.h"
#include "UObject/TextProperty.h"
#include "UObject/Package.h"

static FString JsonEscape(const FString& Input);
static FString JsonError(const FString& Message);
static FString JsonStringArray(const TArray<FString>& Values);
static UClass* FindWidgetClassByName(const FString& ClassName);
static FString WidgetJson(UWidget* Widget, UWidgetBlueprint* Blueprint);

struct FNamedMaterialProperty
{
	const TCHAR* Name;
	EMaterialProperty Property;
};

static const FNamedMaterialProperty GNamedMaterialProperties[] = {
	{TEXT("BaseColor"), MP_BaseColor},
	{TEXT("Metallic"), MP_Metallic},
	{TEXT("Specular"), MP_Specular},
	{TEXT("Roughness"), MP_Roughness},
	{TEXT("Normal"), MP_Normal},
	{TEXT("EmissiveColor"), MP_EmissiveColor},
	{TEXT("Opacity"), MP_Opacity},
	{TEXT("OpacityMask"), MP_OpacityMask},
	{TEXT("WorldPositionOffset"), MP_WorldPositionOffset},
	{TEXT("AmbientOcclusion"), MP_AmbientOcclusion},
	{TEXT("SubsurfaceColor"), MP_SubsurfaceColor},
	{TEXT("MaterialAttributes"), MP_MaterialAttributes},
};

static bool ResolveMaterialProperty(const FString& PropertyName, EMaterialProperty& OutProperty)
{
	FString Name = PropertyName;
	Name.TrimStartAndEndInline();
	Name.ReplaceInline(TEXT("_"), TEXT(""));
	Name.ReplaceInline(TEXT(" "), TEXT(""));
	if (Name.StartsWith(TEXT("MP"), ESearchCase::IgnoreCase))
	{
		Name.RightChopInline(2);
	}
	for (const FNamedMaterialProperty& NamedProperty : GNamedMaterialProperties)
	{
		if (Name.Equals(NamedProperty.Name, ESearchCase::IgnoreCase))
		{
			OutProperty = NamedProperty.Property;
			return true;
		}
	}
	return false;
}

static bool IsUnsafeSetMaterialAttributesProperty(const FString& PropertyName)
{
	FString Name = PropertyName;
	Name.TrimStartAndEndInline();
	Name.ReplaceInline(TEXT("_"), TEXT(""));
	Name.ReplaceInline(TEXT(" "), TEXT(""));
	return Name.Equals(TEXT("AttributeSetTypes"), ESearchCase::IgnoreCase)
		|| Name.Equals(TEXT("Inputs"), ESearchCase::IgnoreCase);
}

struct FResolvedMaterialExpressionProperty
{
	FString RequestedName;
	FString Value;
	FProperty* Property = nullptr;
};

static FString NormalizeReflectedPropertyName(const FString& Name)
{
	FString Normalized;
	for (int32 Index = 0; Index < Name.Len(); ++Index)
	{
		const TCHAR Character = Name[Index];
		if (FChar::IsAlnum(Character))
		{
			Normalized.AppendChar(FChar::ToLower(Character));
		}
	}
	return Normalized;
}

static FProperty* FindMaterialExpressionProperty(
	UClass* ExpressionClass,
	const FString& RequestedName,
	FString& OutResolvedName)
{
	if (!ExpressionClass) return nullptr;

	if (FProperty* ExactProperty = FindFProperty<FProperty>(ExpressionClass, FName(*RequestedName)))
	{
		OutResolvedName = ExactProperty->GetName();
		return ExactProperty;
	}

	const FString NormalizedRequestedName = NormalizeReflectedPropertyName(RequestedName);
	if (NormalizedRequestedName.IsEmpty()) return nullptr;

	FProperty* BooleanPrefixMatch = nullptr;
	for (TFieldIterator<FProperty> It(ExpressionClass); It; ++It)
	{
		FProperty* Candidate = *It;
		const FString NormalizedCandidateName = NormalizeReflectedPropertyName(Candidate->GetName());
		if (NormalizedCandidateName == NormalizedRequestedName)
		{
			OutResolvedName = Candidate->GetName();
			return Candidate;
		}
		if (!BooleanPrefixMatch
			&& CastField<FBoolProperty>(Candidate)
			&& NormalizedCandidateName.StartsWith(TEXT("b"))
			&& NormalizedCandidateName.Mid(1) == NormalizedRequestedName)
		{
			BooleanPrefixMatch = Candidate;
		}
	}

	if (BooleanPrefixMatch)
	{
		OutResolvedName = BooleanPrefixMatch->GetName();
	}
	return BooleanPrefixMatch;
}

static FString MaterialNodePropertyError(
	UMaterial* Material,
	const FString& ExpressionClass,
	const TArray<FString>& PropertyErrors,
	bool bRolledBack)
{
	FString Json = TEXT("{\"error\":\"Requested material node properties could not be applied; no asset was saved.\"");
	Json += TEXT(",\"code\":\"MATERIAL_NODE_PROPERTIES_UNAPPLIED\"");
	Json += TEXT(",\"material\":\"") + JsonEscape(Material ? Material->GetPathName() : TEXT("")) + TEXT("\"");
	Json += TEXT(",\"expression_class\":\"") + JsonEscape(ExpressionClass) + TEXT("\"");
	Json += TEXT(",\"property_errors\":") + JsonStringArray(PropertyErrors);
	Json += TEXT(",\"node_created\":false,\"saved\":false");
	Json += bRolledBack ? TEXT(",\"rolled_back\":true}") : TEXT(",\"rolled_back\":false}");
	return Json;
}

static int32 CreateOrGetSetMaterialAttributeInput(
	UMaterialExpressionSetMaterialAttributes* SetAttributes,
	EMaterialProperty Property)
{
	if (!SetAttributes) return INDEX_NONE;
#if ENGINE_MAJOR_VERSION > 5 || (ENGINE_MAJOR_VERSION == 5 && ENGINE_MINOR_VERSION >= 5)
	return SetAttributes->CreateOrGetInputAttribute(Property);
#else
	if (Property == MP_MaterialAttributes) return 0;

	const FGuid AttributeId = FMaterialAttributeDefinitionMap::GetID(Property);
	int32 AttributeIndex = INDEX_NONE;
	if (SetAttributes->AttributeSetTypes.Find(AttributeId, AttributeIndex))
	{
		return AttributeIndex + 1;
	}

	const int32 SetTypesIndex = SetAttributes->AttributeSetTypes.Add(AttributeId);
	if (SetTypesIndex == INDEX_NONE) return INDEX_NONE;

	// UE 4.26 has no CreateOrGetInputAttribute helper. Mirror UE5's native
	// implementation so AttributeSetTypes and Inputs always change together.
	SetAttributes->PreEditChange(nullptr);
	const int32 InputIndex = SetAttributes->Inputs.Add(FExpressionInput());
	if (SetAttributes->Inputs.IsValidIndex(InputIndex))
	{
		SetAttributes->Inputs[InputIndex].InputName = FName(
			*FMaterialAttributeDefinitionMap::GetDisplayNameForMaterial(AttributeId, SetAttributes->Material).ToString());
	}
	return InputIndex;
#endif
}

class FMaterialResourceExtractSource : public FMaterialResource
{
public:
#if ENGINE_MAJOR_VERSION >= 5
	virtual void SetupExtraCompilationSettings(FExtraShaderCompilerSettings& Settings) const override
#else
	virtual void SetupExtaCompilationSettings(const EShaderPlatform Platform, FExtraShaderCompilerSettings& Settings) const override
#endif
	{
		Settings.bExtractShaderSource = true;
	}
};

#if ENGINE_MAJOR_VERSION < 5
static const TCHAR* GetTextureSourceFormatName426(ETextureSourceFormat Format)
{
	switch (Format)
	{
	case TSF_G8: return TEXT("TSF_G8");
	case TSF_BGRA8: return TEXT("TSF_BGRA8");
	case TSF_BGRE8: return TEXT("TSF_BGRE8");
	case TSF_RGBA16: return TEXT("TSF_RGBA16");
	case TSF_RGBA16F: return TEXT("TSF_RGBA16F");
	case TSF_RGBA8: return TEXT("TSF_RGBA8");
	case TSF_RGBE8: return TEXT("TSF_RGBE8");
	case TSF_G16: return TEXT("TSF_G16");
	default: return TEXT("TSF_Invalid");
	}
}

static int32 GetTextureSourceNumComponents426(ETextureSourceFormat Format)
{
	switch (Format)
	{
	case TSF_G8:
	case TSF_G16:
		return 1;
	case TSF_BGRA8:
	case TSF_BGRE8:
	case TSF_RGBA16:
	case TSF_RGBA16F:
	case TSF_RGBA8:
	case TSF_RGBE8:
		return 4;
	default:
		return 0;
	}
}
#endif

TArray<FString> UCliAnythingBridgeLibrary::GetMaterialCompileErrors(UObject* Asset)
{
	TArray<FString> Result;
	UMaterialInterface* Material = Cast<UMaterialInterface>(Asset);
	if (!Material)
	{
		if (UMaterialFunctionInterface* MaterialFunction = Cast<UMaterialFunctionInterface>(Asset))
		{
			Material = MaterialFunction->GetPreviewMaterial();
		}
	}
	if (!Material) return Result;
	UMaterial* BaseMat = Material->GetMaterial();
	if (!BaseMat) return Result;
	const EShaderPlatform Platform = GMaxRHIShaderPlatform;
	for (int32 QualityLevel = 0; QualityLevel < EMaterialQualityLevel::Num; ++QualityLevel)
	{
#if ENGINE_MAJOR_VERSION >= 5
		const FMaterialResource* Resource = BaseMat->GetMaterialResource(Platform, static_cast<EMaterialQualityLevel::Type>(QualityLevel));
#else
		const FMaterialResource* Resource = BaseMat->GetMaterialResource(GMaxRHIFeatureLevel, static_cast<EMaterialQualityLevel::Type>(QualityLevel));
#endif
		if (!Resource) continue;
		for (const FString& Error : Resource->GetCompileErrors()) { Result.AddUnique(Error); }
	}
	return Result;
}

FString UCliAnythingBridgeLibrary::GetMaterialCompileStatus(UObject* Asset)
{
	UMaterialInterface* Material = Cast<UMaterialInterface>(Asset);
	if (!Material)
	{
		if (UMaterialFunctionInterface* Function = Cast<UMaterialFunctionInterface>(Asset))
		{
			Material = Function->GetPreviewMaterial();
		}
	}
	if (!Material || !Material->GetMaterial()) return JsonError(TEXT("Material resource owner is unavailable"));

	// Preserve the base-material scope of GetMaterialCompileErrors, and expose it.
	UMaterial* BaseMaterial = Material->GetMaterial();
#if ENGINE_MAJOR_VERSION >= 5
	const EShaderPlatform Platform = GEditor ? GEditor->GetActiveShaderPlatform() : GMaxRHIShaderPlatform;
	const FString PlatformName = FDataDrivenShaderPlatformInfo::GetName(Platform).ToString();
	const TCHAR* PlatformSource = TEXT("active_editor");
#else
	const EShaderPlatform Platform = GMaxRHIShaderPlatform;
	const FString PlatformName = LegacyShaderPlatformToShaderFormat(Platform).ToString();
	const TCHAR* PlatformSource = TEXT("host_rhi");
#endif
	FString Json = TEXT("{\"checked_platform\":\"") + JsonEscape(PlatformName) + TEXT("\"");
	Json += TEXT(",\"platform_source\":\"") + FString(PlatformSource) + TEXT("\"");
	Json += TEXT(",\"resource_scope\":\"base_material\",\"resource_material\":\"") + JsonEscape(BaseMaterial->GetPathName()) + TEXT("\"");
	Json += TEXT(",\"resources\":[");
	TArray<FString> Errors;
	for (int32 Quality = 0; Quality < EMaterialQualityLevel::Num; ++Quality)
	{
#if ENGINE_MAJOR_VERSION >= 5
		const FMaterialResource* Resource = BaseMaterial->GetMaterialResource(Platform, static_cast<EMaterialQualityLevel::Type>(Quality));
#else
		const FMaterialResource* Resource = BaseMaterial->GetMaterialResource(GMaxRHIFeatureLevel, static_cast<EMaterialQualityLevel::Type>(Quality));
#endif
		if (Quality) Json += TEXT(",");
		Json += FString::Printf(TEXT("{\"quality_level\":%d,\"resource_present\":%s"), Quality, Resource ? TEXT("true") : TEXT("false"));
		if (Resource)
		{
			const bool bFinished = Resource->IsCompilationFinished();
			const bool bValid = Resource->HasValidGameThreadShaderMap();
			Json += FString::Printf(TEXT(",\"compilation_finished\":%s,\"shader_map_valid\":%s"), bFinished ? TEXT("true") : TEXT("false"), bValid ? TEXT("true") : TEXT("false"));
			Json += TEXT(",\"errors\":") + JsonStringArray(Resource->GetCompileErrors());
			for (const FString& Error : Resource->GetCompileErrors()) Errors.AddUnique(Error);
		}
		Json += TEXT("}");
	}
	Json += TEXT("],\"errors\":") + JsonStringArray(Errors) + TEXT("}");
	return Json;
}

static FString BuildMaterialGraphJson(
	UObject* Asset,
	const TArray<UMaterialExpression*>& Expressions,
	UMaterial* Material)
{
	TArray<FString> FunctionInputs;
	TArray<FString> FunctionOutputs;
	FString NodesJson = TEXT("[");
	FString TexturesJson = TEXT("[");
	bool bFirstNode = true;
	bool bFirstTexture = true;
	int32 NodeCount = 0;
	int32 TextureCount = 0;
	for (UMaterialExpression* Expression : Expressions)
	{
		if (!Expression) continue;
		++NodeCount;
		if (!bFirstNode) NodesJson += TEXT(",");
		bFirstNode = false;

		const FString ClassName = Expression->GetClass()->GetName();
		TArray<FString> Captions;
		Expression->GetCaption(Captions);

		NodesJson += TEXT("{\"name\":\"") + JsonEscape(Expression->GetName()) + TEXT("\"");
		NodesJson += TEXT(",\"type\":\"") + JsonEscape(ClassName) + TEXT("\"");
		NodesJson += TEXT(",\"captions\":") + JsonStringArray(Captions);
		if (!Expression->Desc.IsEmpty())
		{
			NodesJson += TEXT(",\"desc\":\"") + JsonEscape(Expression->Desc) + TEXT("\"");
		}

		if (const UMaterialExpressionCustom* Custom = Cast<UMaterialExpressionCustom>(Expression))
		{
			FString TrimmedCode = Custom->Code.TrimStartAndEnd();
			TArray<FString> CodeLines;
			TrimmedCode.ParseIntoArrayLines(CodeLines, false);
			const int32 PreviewLineCount = FMath::Min(10, CodeLines.Num());
			TArray<FString> PreviewLines;
			for (int32 Index = 0; Index < PreviewLineCount; ++Index)
			{
				PreviewLines.Add(CodeLines[Index]);
			}
			FString Preview = FString::Join(PreviewLines, TEXT("\n"));
			if (CodeLines.Num() > PreviewLineCount)
			{
				Preview += FString::Printf(
					TEXT("\n// ... (%d) more lines"),
					CodeLines.Num() - PreviewLineCount);
			}
			NodesJson += FString::Printf(TEXT(",\"code_lines\":%d"), CodeLines.Num());
			NodesJson += TEXT(",\"code_preview\":\"") + JsonEscape(Preview) + TEXT("\"");

			const UEnum* OutputTypeEnum = StaticEnum<ECustomMaterialOutputType>();
			if (OutputTypeEnum)
			{
				NodesJson += TEXT(",\"output_type\":\"")
					+ JsonEscape(OutputTypeEnum->GetNameStringByValue(Custom->OutputType.GetValue()))
					+ TEXT("\"");
			}
			TArray<FString> CustomInputs;
			for (const FCustomInput& Input : Custom->Inputs)
			{
				CustomInputs.Add(Input.InputName.ToString());
			}
			NodesJson += TEXT(",\"inputs\":") + JsonStringArray(CustomInputs);
		}
		NodesJson += TEXT("}");

		if (const UMaterialExpressionTextureBase* TextureExpression = Cast<UMaterialExpressionTextureBase>(Expression))
		{
			++TextureCount;
			if (!bFirstTexture) TexturesJson += TEXT(",");
			bFirstTexture = false;
			TexturesJson += TEXT("{\"node_type\":\"") + JsonEscape(ClassName) + TEXT("\"");
			if (const UTexture* Texture = TextureExpression->Texture)
			{
				TexturesJson += TEXT(",\"name\":\"") + JsonEscape(Texture->GetName()) + TEXT("\"");
				TexturesJson += TEXT(",\"path\":\"") + JsonEscape(Texture->GetPathName()) + TEXT("\"");
				if (const UTexture2D* Texture2D = Cast<UTexture2D>(Texture))
				{
					TexturesJson += FString::Printf(
						TEXT(",\"size_x\":%d,\"size_y\":%d"),
						Texture2D->GetSizeX(),
						Texture2D->GetSizeY());
				}
			}
			else
			{
				TexturesJson += TEXT(",\"name\":null,\"path\":null");
			}
			TexturesJson += TEXT("}");
		}

		if (ClassName.Contains(TEXT("MaterialExpressionFunctionInput")))
		{
			FunctionInputs.Add(Expression->GetName());
		}
		else if (ClassName.Contains(TEXT("MaterialExpressionFunctionOutput")))
		{
			FunctionOutputs.Add(Expression->GetName());
		}
	}
	NodesJson += TEXT("]");
	TexturesJson += TEXT("]");

	FString EdgesJson = TEXT("[");
	bool bFirstEdge = true;
	for (UMaterialExpression* ToExpression : Expressions)
	{
		if (!ToExpression) continue;
		const UMaterialExpressionSetMaterialAttributes* SetAttributes = Cast<UMaterialExpressionSetMaterialAttributes>(ToExpression);
		const int32 BoundedInputCount = SetAttributes ? SetAttributes->Inputs.Num() : INDEX_NONE;
		for (int32 InputIndex = 0; BoundedInputCount == INDEX_NONE || InputIndex < BoundedInputCount; ++InputIndex)
		{
			const FExpressionInput* Input = ToExpression->GetInput(InputIndex);
			if (!Input) break;
			if (!Input->Expression) continue;

			FString ToInputName = ToExpression->GetInputName(InputIndex).ToString();
			if (ToInputName.IsEmpty())
			{
				ToInputName = Input->InputName.ToString();
			}

			FString FromOutputName;
			TArray<FExpressionOutput>& Outputs = Input->Expression->GetOutputs();
			if (Outputs.IsValidIndex(Input->OutputIndex))
			{
				FromOutputName = Outputs[Input->OutputIndex].OutputName.ToString();
			}

			if (!bFirstEdge) EdgesJson += TEXT(",");
			bFirstEdge = false;
			EdgesJson += TEXT("{\"from_node\":\"") + JsonEscape(Input->Expression->GetName()) + TEXT("\"");
			EdgesJson += TEXT(",\"to_node\":\"") + JsonEscape(ToExpression->GetName()) + TEXT("\"");
			EdgesJson += FString::Printf(TEXT(",\"to_input_index\":%d,\"from_output_index\":%d"), InputIndex, Input->OutputIndex);
			EdgesJson += TEXT(",\"to_input\":\"") + JsonEscape(ToInputName) + TEXT("\"");
			EdgesJson += TEXT(",\"from_output\":\"") + JsonEscape(FromOutputName) + TEXT("\"}");
		}
	}
	EdgesJson += TEXT("]");

	FString Json = TEXT("{\"status\":\"ok\",\"name\":\"") + JsonEscape(Asset->GetName()) + TEXT("\"");
	Json += TEXT(",\"path\":\"") + JsonEscape(Asset->GetPathName()) + TEXT("\"");
	Json += TEXT(",\"class\":\"") + JsonEscape(Asset->GetClass()->GetName()) + TEXT("\"");
	Json += TEXT(",\"material\":\"") + JsonEscape(Asset->GetPathName()) + TEXT("\"");
	Json += TEXT(",\"nodes\":") + NodesJson;
	Json += FString::Printf(TEXT(",\"node_count\":%d"), NodeCount);
	Json += TEXT(",\"edges\":") + EdgesJson;
	Json += TEXT(",\"textures\":") + TexturesJson;
	Json += FString::Printf(TEXT(",\"texture_sample_count\":%d"), TextureCount);

	if (Material)
	{
		Json += TEXT(",\"blend_mode\":\"")
			+ JsonEscape(StaticEnum<EBlendMode>()->GetNameStringByValue(Material->BlendMode.GetValue()))
			+ TEXT("\"");
		Json += TEXT(",\"material_domain\":\"")
			+ JsonEscape(StaticEnum<EMaterialDomain>()->GetNameStringByValue(Material->MaterialDomain.GetValue()))
			+ TEXT("\"");
		Json += TEXT(",\"shading_model\":\"")
			+ JsonEscape(GetShadingModelFieldString(Material->GetShadingModels()))
			+ TEXT("\"");
		Json += Material->TwoSided ? TEXT(",\"two_sided\":true") : TEXT(",\"two_sided\":false");
		Json += Material->bUseMaterialAttributes
			? TEXT(",\"use_material_attributes\":true")
			: TEXT(",\"use_material_attributes\":false");

		FString OutputsJson = TEXT("{");
		bool bFirstOutput = true;
		for (const FNamedMaterialProperty& NamedProperty : GNamedMaterialProperties)
		{
			FExpressionInput* Input = Material->GetExpressionInputForProperty(NamedProperty.Property);
			if (!Input || !Input->Expression) continue;

			FString OutputName;
			TArray<FExpressionOutput>& Outputs = Input->Expression->GetOutputs();
			if (Outputs.IsValidIndex(Input->OutputIndex))
			{
				OutputName = Outputs[Input->OutputIndex].OutputName.ToString();
			}

			if (!bFirstOutput) OutputsJson += TEXT(",");
			bFirstOutput = false;
			OutputsJson += TEXT("\"") + JsonEscape(NamedProperty.Name) + TEXT("\":{");
			OutputsJson += TEXT("\"node\":\"") + JsonEscape(Input->Expression->GetName()) + TEXT("\"");
			OutputsJson += TEXT(",\"node_type\":\"") + JsonEscape(Input->Expression->GetClass()->GetName()) + TEXT("\"");
			OutputsJson += TEXT(",\"output\":\"") + JsonEscape(OutputName) + TEXT("\"}");
		}
		OutputsJson += TEXT("}");
		Json += TEXT(",\"material_outputs\":") + OutputsJson;
	}
	else
	{
		Json += TEXT(",\"function_inputs\":") + JsonStringArray(FunctionInputs);
		Json += TEXT(",\"function_outputs\":") + JsonStringArray(FunctionOutputs);
	}
	Json += TEXT("}");
	return Json;
}

static FString BuildMaterialInstanceJson(UMaterialInstanceConstant* Instance)
{
	FString Json = TEXT("{\"status\":\"ok\",\"name\":\"") + JsonEscape(Instance->GetName()) + TEXT("\"");
	Json += TEXT(",\"path\":\"") + JsonEscape(Instance->GetPathName()) + TEXT("\"");
	Json += TEXT(",\"class\":\"") + JsonEscape(Instance->GetClass()->GetName()) + TEXT("\"");
	Json += TEXT(",\"parent\":");
	if (Instance->Parent)
	{
		Json += TEXT("\"") + JsonEscape(Instance->Parent->GetPathName()) + TEXT("\"");
	}
	else
	{
		Json += TEXT("null");
	}

	Json += TEXT(",\"scalar_parameters\":[");
	for (int32 Index = 0; Index < Instance->ScalarParameterValues.Num(); ++Index)
	{
		const FScalarParameterValue& Parameter = Instance->ScalarParameterValues[Index];
		if (Index > 0) Json += TEXT(",");
		Json += TEXT("{\"name\":\"") + JsonEscape(Parameter.ParameterInfo.Name.ToString()) + TEXT("\"");
		Json += TEXT(",\"value\":") + FString::SanitizeFloat(Parameter.ParameterValue) + TEXT("}");
	}
	Json += TEXT("]");

	Json += TEXT(",\"vector_parameters\":[");
	for (int32 Index = 0; Index < Instance->VectorParameterValues.Num(); ++Index)
	{
		const FVectorParameterValue& Parameter = Instance->VectorParameterValues[Index];
		const FLinearColor& Value = Parameter.ParameterValue;
		if (Index > 0) Json += TEXT(",");
		Json += TEXT("{\"name\":\"") + JsonEscape(Parameter.ParameterInfo.Name.ToString()) + TEXT("\"");
		Json += TEXT(",\"value\":{\"r\":") + FString::SanitizeFloat(Value.R);
		Json += TEXT(",\"g\":") + FString::SanitizeFloat(Value.G);
		Json += TEXT(",\"b\":") + FString::SanitizeFloat(Value.B);
		Json += TEXT(",\"a\":") + FString::SanitizeFloat(Value.A) + TEXT("}}");
	}
	Json += TEXT("]");

	Json += TEXT(",\"texture_parameters\":[");
	for (int32 Index = 0; Index < Instance->TextureParameterValues.Num(); ++Index)
	{
		const FTextureParameterValue& Parameter = Instance->TextureParameterValues[Index];
		if (Index > 0) Json += TEXT(",");
		Json += TEXT("{\"name\":\"") + JsonEscape(Parameter.ParameterInfo.Name.ToString()) + TEXT("\"");
		Json += TEXT(",\"texture\":");
		if (Parameter.ParameterValue)
		{
			Json += TEXT("\"") + JsonEscape(Parameter.ParameterValue->GetPathName()) + TEXT("\"");
		}
		else
		{
			Json += TEXT("null");
		}
		Json += TEXT("}");
	}
	Json += TEXT("]}");
	return Json;
}

static TArray<UMaterialExpression*> GetMaterialExpressions(UMaterial* Material)
{
	TArray<UMaterialExpression*> Expressions;
	if (!Material) return Expressions;
#if ENGINE_MAJOR_VERSION >= 5
	for (UMaterialExpression* Expression : Material->GetExpressions())
	{
		Expressions.Add(Expression);
	}
#else
	Expressions.Append(Material->Expressions);
#endif
	return Expressions;
}

static UMaterialExpression* FindMaterialExpression(UMaterial* Material, const FString& NodeName, TArray<FString>* AvailableNodes = nullptr)
{
	UMaterialExpression* Found = nullptr;
	for (UMaterialExpression* Expression : GetMaterialExpressions(Material))
	{
		if (!Expression) continue;
		if (AvailableNodes) AvailableNodes->Add(Expression->GetName());
		if (!Found && Expression->GetName().Equals(NodeName, ESearchCase::CaseSensitive)) Found = Expression;
	}
	return Found;
}

static void RecompileEditedMaterial(UMaterial* Material)
{
	UMaterialEditingLibrary::RecompileMaterial(Material);
	Material->MarkPackageDirty();
}

FString UCliAnythingBridgeLibrary::GetMaterialInfo(UObject* Asset)
{
	if (!Asset)
	{
		return TEXT("{\"error\":\"Material asset is null\",\"code\":\"MATERIAL_NOT_FOUND\"}");
	}

	if (UMaterial* Material = Cast<UMaterial>(Asset))
	{
		TArray<UMaterialExpression*> Expressions = GetMaterialExpressions(Material);
		return BuildMaterialGraphJson(Material, Expressions, Material);
	}

	if (UMaterialFunctionInterface* MaterialFunction = Cast<UMaterialFunctionInterface>(Asset))
	{
		TArray<UMaterialExpression*> Expressions;
#if ENGINE_MAJOR_VERSION >= 5
		for (UMaterialExpression* Expression : MaterialFunction->GetExpressions())
		{
			Expressions.Add(Expression);
		}
#else
		const TArray<UMaterialExpression*>* FunctionExpressions = MaterialFunction->GetFunctionExpressions();
		if (!FunctionExpressions)
		{
			return JsonError(TEXT("MaterialFunction expressions are unavailable"));
		}
		Expressions.Append(*FunctionExpressions);
#endif
		return BuildMaterialGraphJson(MaterialFunction, Expressions, nullptr);
	}

	if (UMaterialInstanceConstant* Instance = Cast<UMaterialInstanceConstant>(Asset))
	{
		return BuildMaterialInstanceJson(Instance);
	}

	return TEXT("{\"error\":\"Unsupported material asset class: ")
		+ JsonEscape(Asset->GetClass()->GetName())
		+ TEXT("\",\"code\":\"MATERIAL_INFO_UNSUPPORTED_CLASS\",\"asset_class\":\"")
		+ JsonEscape(Asset->GetClass()->GetName())
		+ TEXT("\",\"supported_classes\":[\"Material\",\"MaterialFunction\",\"MaterialInstanceConstant\"]}");
}

static bool IsHlslIdentifier(const FString& Name)
{
	if (Name.IsEmpty() || !(FChar::IsAlpha(Name[0]) || Name[0] == TEXT('_'))) return false;
	for (int32 Index = 1; Index < Name.Len(); ++Index)
	{
		if (!(FChar::IsAlnum(Name[Index]) || Name[Index] == TEXT('_'))) return false;
	}
	return true;
}

static FString ReplaceHlslIdentifier(const FString& Code, const FString& OldName, const FString& NewName, bool& bChanged)
{
	bChanged = false;
	if (OldName.IsEmpty() || OldName == NewName) return Code;
	FString Result;
	for (int32 Index = 0; Index < Code.Len();)
	{
		const bool bMatches = Code.Mid(Index, OldName.Len()) == OldName;
		const bool bLeftBoundary = Index == 0 || !(FChar::IsAlnum(Code[Index - 1]) || Code[Index - 1] == TEXT('_'));
		const int32 RightIndex = Index + OldName.Len();
		const bool bRightBoundary = RightIndex >= Code.Len() || !(FChar::IsAlnum(Code[RightIndex]) || Code[RightIndex] == TEXT('_'));
		if (bMatches && bLeftBoundary && bRightBoundary)
		{
			Result += NewName;
			Index = RightIndex;
			bChanged = true;
		}
		else
		{
			Result.AppendChar(Code[Index++]);
		}
	}
	return Result;
}

FString UCliAnythingBridgeLibrary::AddMaterialExpression(
	UMaterial* Material,
	const FString& ExpressionClass,
	int32 PosX,
	int32 PosY,
	const TMap<FString, FString>& Properties,
	const TArray<FString>& InputNames)
{
	if (!Material) return JsonError(TEXT("Material is null"));

	UClass* FoundClass = nullptr;
	for (TObjectIterator<UClass> It; It; ++It)
	{
		if (It->GetName() == ExpressionClass && It->IsChildOf(UMaterialExpression::StaticClass()))
		{
			FoundClass = *It;
			break;
		}
	}
	if (!FoundClass || FoundClass->HasAnyClassFlags(CLASS_Abstract))
	{
		return JsonError(TEXT("Material expression class not found: ") + ExpressionClass);
	}
	if (FoundClass->IsChildOf(UMaterialExpressionSetMaterialAttributes::StaticClass()))
	{
		for (const TPair<FString, FString>& Pair : Properties)
		{
			if (IsUnsafeSetMaterialAttributesProperty(Pair.Key))
			{
				return TEXT("{\"error\":\"") + JsonEscape(Pair.Key)
					+ TEXT(" cannot be written independently on MaterialExpressionSetMaterialAttributes; use material connect --to-input <attribute>\"")
					+ TEXT(",\"code\":\"MATERIAL_SET_ATTRIBUTES_UNSAFE_PROPERTY\",\"unsafe_property\":\"")
					+ JsonEscape(Pair.Key)
					+ TEXT("\",\"safe_command\":\"material connect <material> --from <node> --to <set-attributes-node> --to-input <attribute>\"}");
			}
		}
	}

	TArray<FResolvedMaterialExpressionProperty> ResolvedProperties;
	TArray<FString> PropertyErrors;
	for (const TPair<FString, FString>& Pair : Properties)
	{
		FString ResolvedName;
		FProperty* Property = FindMaterialExpressionProperty(FoundClass, Pair.Key, ResolvedName);
		if (!Property)
		{
			PropertyErrors.Add(Pair.Key + TEXT(": property not found"));
			continue;
		}

		FResolvedMaterialExpressionProperty Resolved;
		Resolved.RequestedName = Pair.Key;
		Resolved.Value = Pair.Value;
		Resolved.Property = Property;
		ResolvedProperties.Add(Resolved);
	}
	if (InputNames.Num() > 0 && !FoundClass->IsChildOf(UMaterialExpressionCustom::StaticClass()))
	{
		PropertyErrors.Add(TEXT("inputs: input names require MaterialExpressionCustom"));
	}
	if (PropertyErrors.Num() > 0)
	{
		return MaterialNodePropertyError(Material, ExpressionClass, PropertyErrors, false);
	}

	UPackage* MaterialPackage = Material->GetOutermost();
	const bool bPackageWasDirty = MaterialPackage && MaterialPackage->IsDirty();
	Material->Modify();
	UMaterialExpression* Expression = UMaterialEditingLibrary::CreateMaterialExpression(Material, FoundClass, PosX, PosY);
	if (!Expression) return JsonError(TEXT("CreateMaterialExpression failed: ") + ExpressionClass);

	Expression->Modify();
	for (const FResolvedMaterialExpressionProperty& Resolved : ResolvedProperties)
	{
		FProperty* Property = Resolved.Property;
		void* ValuePtr = Property->ContainerPtrToValuePtr<void>(Expression);
		bool bSet = true;
		if (FStrProperty* StringProperty = CastField<FStrProperty>(Property))
		{
			StringProperty->SetPropertyValue(ValuePtr, Resolved.Value);
		}
		else if (FNameProperty* NameProperty = CastField<FNameProperty>(Property))
		{
			NameProperty->SetPropertyValue(ValuePtr, FName(*Resolved.Value));
		}
		else if (FBoolProperty* BoolProperty = CastField<FBoolProperty>(Property))
		{
			if (Resolved.Value.Equals(TEXT("true"), ESearchCase::IgnoreCase) || Resolved.Value == TEXT("1")) BoolProperty->SetPropertyValue(ValuePtr, true);
			else if (Resolved.Value.Equals(TEXT("false"), ESearchCase::IgnoreCase) || Resolved.Value == TEXT("0")) BoolProperty->SetPropertyValue(ValuePtr, false);
			else bSet = false;
		}
		else if (FNumericProperty* NumericProperty = CastField<FNumericProperty>(Property))
		{
			if (NumericProperty->IsFloatingPoint()) NumericProperty->SetFloatingPointPropertyValue(ValuePtr, FCString::Atod(*Resolved.Value));
			else if (NumericProperty->IsInteger()) NumericProperty->SetIntPropertyValue(ValuePtr, FCString::Atoi64(*Resolved.Value));
			else bSet = false;
		}
		else
		{
#if ENGINE_MAJOR_VERSION >= 5
			bSet = Property->ImportText_Direct(*Resolved.Value, ValuePtr, Expression, PPF_None) != nullptr;
#else
			bSet = Property->ImportText(*Resolved.Value, ValuePtr, PPF_None, Expression) != nullptr;
#endif
		}
		if (!bSet) PropertyErrors.Add(Resolved.RequestedName + TEXT("=") + Resolved.Value + TEXT(": invalid value"));
	}

	if (InputNames.Num() > 0)
	{
		UMaterialExpressionCustom* Custom = Cast<UMaterialExpressionCustom>(Expression);
		if (Custom)
		{
			Custom->Inputs.Reset();
			for (const FString& InputName : InputNames)
			{
				FCustomInput Input;
				Input.InputName = FName(*InputName);
				Custom->Inputs.Add(Input);
			}
		}
	}
	if (PropertyErrors.Num() > 0)
	{
		UMaterialEditingLibrary::DeleteMaterialExpression(Material, Expression);
		if (MaterialPackage && !bPackageWasDirty)
		{
			MaterialPackage->SetDirtyFlag(false);
		}
		return MaterialNodePropertyError(Material, ExpressionClass, PropertyErrors, true);
	}

	Expression->PostEditChange();
	RecompileEditedMaterial(Material);
	FString Json = TEXT("{\"status\":\"ok\",\"action\":\"add_node\",\"material\":\"") + JsonEscape(Material->GetPathName()) + TEXT("\"");
	Json += TEXT(",\"node\":{\"name\":\"") + JsonEscape(Expression->GetName()) + TEXT("\",\"type\":\"") + JsonEscape(Expression->GetClass()->GetName()) + TEXT("\"}");
	if (ResolvedProperties.Num() > 0)
	{
		Json += TEXT(",\"applied_properties\":{");
		for (int32 Index = 0; Index < ResolvedProperties.Num(); ++Index)
		{
			if (Index > 0) Json += TEXT(",");
			const FResolvedMaterialExpressionProperty& Resolved = ResolvedProperties[Index];
			Json += TEXT("\"") + JsonEscape(Resolved.RequestedName) + TEXT("\":\"")
				+ JsonEscape(Resolved.Property->GetName()) + TEXT("\"");
		}
		Json += TEXT("}");
	}
	Json += TEXT("}");
	return Json;
}

FString UCliAnythingBridgeLibrary::DeleteMaterialExpression(UMaterial* Material, const FString& NodeName)
{
	if (!Material) return JsonError(TEXT("Material is null"));
	TArray<FString> AvailableNodes;
	UMaterialExpression* Expression = FindMaterialExpression(Material, NodeName, &AvailableNodes);
	if (!Expression)
	{
		return TEXT("{\"error\":\"Node not found: ") + JsonEscape(NodeName) + TEXT("\",\"available_nodes\":") + JsonStringArray(AvailableNodes) + TEXT("}");
	}
	const FString DeletedName = Expression->GetName();
	Material->Modify();
	Expression->Modify();
	UMaterialEditingLibrary::DeleteMaterialExpression(Material, Expression);
	RecompileEditedMaterial(Material);
	return TEXT("{\"status\":\"ok\",\"action\":\"delete_node\",\"material\":\"") + JsonEscape(Material->GetPathName()) + TEXT("\",\"deleted_node\":\"") + JsonEscape(DeletedName) + TEXT("\"}");
}

FString UCliAnythingBridgeLibrary::RenameMaterialCustomInput(
	UMaterial* Material,
	const FString& NodeName,
	const FString& OldName,
	const FString& NewName,
	bool bUpdateCode)
{
	if (!Material) return JsonError(TEXT("Material is null"));
	if (!IsHlslIdentifier(NewName)) return JsonError(TEXT("New Custom input name is not a valid HLSL identifier: ") + NewName);
	TArray<FString> AvailableNodes;
	UMaterialExpression* Expression = FindMaterialExpression(Material, NodeName, &AvailableNodes);
	if (!Expression)
	{
		return TEXT("{\"error\":\"Node not found: ") + JsonEscape(NodeName) + TEXT("\",\"available_nodes\":") + JsonStringArray(AvailableNodes) + TEXT("}");
	}
	UMaterialExpressionCustom* Custom = Cast<UMaterialExpressionCustom>(Expression);
	if (!Custom) return JsonError(TEXT("Node is not a MaterialExpressionCustom: ") + NodeName);

	TArray<FString> BeforeNames;
	bool bFound = false;
	for (const FCustomInput& Input : Custom->Inputs)
	{
		const FString InputName = Input.InputName.ToString();
		BeforeNames.Add(InputName);
		bFound |= InputName == OldName;
	}
	if (!bFound)
	{
		return TEXT("{\"error\":\"Custom input not found: ") + JsonEscape(OldName) + TEXT("\",\"inputs\":") + JsonStringArray(BeforeNames) + TEXT("}");
	}
	if (OldName != NewName && BeforeNames.Contains(NewName))
	{
		return TEXT("{\"error\":\"Custom input already exists: ") + JsonEscape(NewName) + TEXT("\",\"inputs\":") + JsonStringArray(BeforeNames) + TEXT("}");
	}

	Material->Modify();
	Custom->Modify();
	for (FCustomInput& Input : Custom->Inputs)
	{
		if (Input.InputName.ToString() == OldName) Input.InputName = FName(*NewName);
	}
	bool bCodeUpdated = false;
	if (bUpdateCode) Custom->Code = ReplaceHlslIdentifier(Custom->Code, OldName, NewName, bCodeUpdated);
	Custom->PostEditChange();
	RecompileEditedMaterial(Material);

	TArray<FString> AfterNames;
	for (const FCustomInput& Input : Custom->Inputs) AfterNames.Add(Input.InputName.ToString());
	FString Json = TEXT("{\"status\":\"ok\",\"action\":\"rename_custom_input\",\"material\":\"") + JsonEscape(Material->GetPathName()) + TEXT("\"");
	Json += TEXT(",\"node\":\"") + JsonEscape(NodeName) + TEXT("\",\"old_name\":\"") + JsonEscape(OldName) + TEXT("\",\"new_name\":\"") + JsonEscape(NewName) + TEXT("\"");
	Json += TEXT(",\"inputs_before\":") + JsonStringArray(BeforeNames) + TEXT(",\"inputs_after\":") + JsonStringArray(AfterNames);
	Json += TEXT(",\"code_updated\":") + FString(bCodeUpdated ? TEXT("true") : TEXT("false")) + TEXT("}");
	return Json;
}

FString UCliAnythingBridgeLibrary::ConnectMaterialExpressions(
	UMaterial* Material,
	const FString& FromNode,
	const FString& FromOutputName,
	const FString& ToNode,
	const FString& ToInputName)
{
	if (!Material) return JsonError(TEXT("Material is null"));
	TArray<FString> AvailableNodes;
	UMaterialExpression* FromExpression = FindMaterialExpression(Material, FromNode, &AvailableNodes);
	UMaterialExpression* ToExpression = FindMaterialExpression(Material, ToNode);
	if (!FromExpression || !ToExpression)
	{
		const FString Missing = !FromExpression ? FromNode : ToNode;
		return TEXT("{\"error\":\"Node not found: ") + JsonEscape(Missing) + TEXT("\",\"available_nodes\":") + JsonStringArray(AvailableNodes) + TEXT("}");
	}
	Material->Modify();
	FString ResolvedToInputName = ToInputName;
	int32 SetAttributeInputIndex = INDEX_NONE;
	bool bSetAttributeInputCreated = false;
	if (UMaterialExpressionSetMaterialAttributes* SetAttributes = Cast<UMaterialExpressionSetMaterialAttributes>(ToExpression))
	{
		FString Wanted = ToInputName;
		Wanted.TrimStartAndEndInline();
		if (!Wanted.IsEmpty())
		{
			EMaterialProperty Property = MP_MAX;
			if (!ResolveMaterialProperty(Wanted, Property))
			{
				return TEXT("{\"error\":\"Unsupported SetMaterialAttributes input: ") + JsonEscape(Wanted)
					+ TEXT("\",\"code\":\"MATERIAL_ATTRIBUTE_NOT_FOUND\"}");
			}
			SetAttributes->Modify();
			const int32 InputCountBefore = SetAttributes->Inputs.Num();
			SetAttributeInputIndex = CreateOrGetSetMaterialAttributeInput(SetAttributes, Property);
			if (!SetAttributes->Inputs.IsValidIndex(SetAttributeInputIndex))
			{
				return JsonError(TEXT("Failed to create SetMaterialAttributes input: ") + Wanted);
			}
			bSetAttributeInputCreated = SetAttributes->Inputs.Num() > InputCountBefore;
			ResolvedToInputName = SetAttributes->GetInputName(SetAttributeInputIndex).ToString();
		}
	}
	if (!UMaterialEditingLibrary::ConnectMaterialExpressions(FromExpression, FromOutputName, ToExpression, ResolvedToInputName))
	{
		return JsonError(TEXT("ConnectMaterialExpressions returned false"));
	}
	RecompileEditedMaterial(Material);
	FString Json = TEXT("{\"status\":\"ok\",\"action\":\"connect\",\"from\":\"") + JsonEscape(FromNode) + TEXT("\"");
	Json += TEXT(",\"from_output\":\"") + JsonEscape(FromOutputName) + TEXT("\",\"to\":\"") + JsonEscape(ToNode) + TEXT("\",\"to_input\":\"") + JsonEscape(ToInputName) + TEXT("\"}");
	if (SetAttributeInputIndex != INDEX_NONE)
	{
		Json.RemoveFromEnd(TEXT("}"));
		Json += FString::Printf(TEXT(",\"set_material_attribute_input\":true,\"input_index\":%d"), SetAttributeInputIndex);
		Json += bSetAttributeInputCreated ? TEXT(",\"input_created\":true}") : TEXT(",\"input_created\":false}");
	}
	return Json;
}

FString UCliAnythingBridgeLibrary::DisconnectMaterialExpression(UMaterial* Material, const FString& ToNode, const FString& ToInputName)
{
	if (!Material) return JsonError(TEXT("Material is null"));
	TArray<FString> AvailableNodes;
	UMaterialExpression* ToExpression = FindMaterialExpression(Material, ToNode, &AvailableNodes);
	if (!ToExpression)
	{
		return TEXT("{\"error\":\"Node not found: ") + JsonEscape(ToNode) + TEXT("\",\"available_nodes\":") + JsonStringArray(AvailableNodes) + TEXT("}");
	}

	FString Wanted = ToInputName;
	Wanted.TrimStartAndEndInline();

	TArray<FString> AvailableInputs;
	int32 TargetIndex = INDEX_NONE;
	FExpressionInput* TargetInput = nullptr;
	if (UMaterialExpressionSetMaterialAttributes* SetAttributes = Cast<UMaterialExpressionSetMaterialAttributes>(ToExpression))
	{
		for (int32 Index = 0; Index < SetAttributes->Inputs.Num(); ++Index)
		{
			AvailableInputs.Add(SetAttributes->GetInputName(Index).ToString());
		}
		if (Wanted.IsEmpty())
		{
			TargetIndex = 0;
		}
		else
		{
			EMaterialProperty Property = MP_MAX;
			if (ResolveMaterialProperty(Wanted, Property))
			{
				if (Property == MP_MaterialAttributes)
				{
					TargetIndex = 0;
				}
				else
				{
					int32 AttributeIndex = INDEX_NONE;
					if (SetAttributes->AttributeSetTypes.Find(FMaterialAttributeDefinitionMap::GetID(Property), AttributeIndex))
					{
						TargetIndex = AttributeIndex + 1;
					}
				}
			}
		}
		if (SetAttributes->Inputs.IsValidIndex(TargetIndex)) TargetInput = &SetAttributes->Inputs[TargetIndex];
	}
	else
	{
		for (int32 Index = 0;; ++Index)
		{
			FExpressionInput* CurrentInput = ToExpression->GetInput(Index);
			if (!CurrentInput)
			{
				break;
			}
			const FString InputName = ToExpression->GetInputName(Index).ToString();
			AvailableInputs.Add(InputName);
			if (TargetIndex == INDEX_NONE && (Wanted.IsEmpty() || InputName.Equals(Wanted, ESearchCase::IgnoreCase)))
			{
				TargetIndex = Index;
				TargetInput = CurrentInput;
				if (!Wanted.IsEmpty())
				{
					break;
				}
			}
		}
	}

	if (AvailableInputs.Num() == 0)
	{
		return JsonError(TEXT("Target expression has no inputs: ") + ToExpression->GetName());
	}

	if (TargetIndex == INDEX_NONE)
	{
		return TEXT("{\"error\":\"Input not found: ") + JsonEscape(Wanted) + TEXT("\",\"available_inputs\":") + JsonStringArray(AvailableInputs) + TEXT("}");
	}

	FExpressionInput* Input = TargetInput;
	if (!Input)
	{
		return JsonError(TEXT("Input pointer is null"));
	}

	const FString InputName = ToExpression->GetInputName(TargetIndex).ToString();
	const bool bHadConnection = Input->Expression != nullptr;
	const FString FromExpressionName = Input->Expression ? Input->Expression->GetName() : FString();
	const FString FromExpressionPath = Input->Expression ? Input->Expression->GetPathName() : FString();

	Material->Modify();
	ToExpression->Modify();

	Input->Expression = nullptr;
	Input->OutputIndex = 0;
	Input->InputName = NAME_None;
	Input->Mask = 0;
	Input->MaskR = 0;
	Input->MaskG = 0;
	Input->MaskB = 0;
	Input->MaskA = 0;

	RecompileEditedMaterial(Material);

	FString Json = TEXT("{\"status\":\"ok\",\"action\":\"disconnect\",\"to_node\":\"") + JsonEscape(ToExpression->GetName()) + TEXT("\"");
	Json += TEXT(",\"to_input\":\"") + JsonEscape(InputName) + TEXT("\"");
	Json += TEXT(",\"had_connection\":") + FString(bHadConnection ? TEXT("true") : TEXT("false"));
	Json += TEXT(",\"disconnected_from\":\"") + JsonEscape(FromExpressionName) + TEXT("\"");
	Json += TEXT(",\"disconnected_from_path\":\"") + JsonEscape(FromExpressionPath) + TEXT("\"}");
	return Json;
}

FString UCliAnythingBridgeLibrary::GetTextureSourceInfo(UTexture2D* Texture)
{
	if (!Texture)
	{
		return JsonError(TEXT("Texture is null"));
	}

	FTextureSource& Source = Texture->Source;
	const int64 SizeX = Source.GetSizeX();
	const int64 SizeY = Source.GetSizeY();
	const int32 NumSlices = Source.GetNumSlices();
	const int32 NumMips = Source.GetNumMips();
	const int32 NumLayers = Source.GetNumLayers();
	const int32 NumBlocks = Source.GetNumBlocks();
	const int64 SizeOnDisk = Source.GetSizeOnDisk();
	const ETextureSourceFormat Format = Source.GetFormat();
	const bool bValidFormat = Format > TSF_Invalid && Format < TSF_MAX;
#if ENGINE_MAJOR_VERSION >= 5
	const bool bHasPayload = Source.HasPayloadData();
	const FTextureSourceFormatInfo& FormatInfo = GTextureSourceFormats[bValidFormat ? Format : TSF_Invalid];
	const FString FormatName = bValidFormat ? FString(FormatInfo.Name) : FString(TEXT("TSF_Invalid"));
	const int32 BytesPerPixel = bValidFormat ? FormatInfo.BytesPerPixel : 0;
	const int32 NumComponents = bValidFormat ? FormatInfo.NumComponents : 0;
#else
	const bool bHasPayload = SizeOnDisk > 0;
	const FString FormatName = GetTextureSourceFormatName426(Format);
	const int32 BytesPerPixel = bValidFormat ? FTextureSource::GetBytesPerPixel(Format) : 0;
	const int32 NumComponents = GetTextureSourceNumComponents426(Format);
#endif

	FString Json = TEXT("{\"status\":\"ok\",\"asset\":\"") + JsonEscape(Texture->GetPathName()) + TEXT("\"");
	Json += FString::Printf(TEXT(",\"source_size\":{\"x\":%lld,\"y\":%lld,\"slices\":%d}"), SizeX, SizeY, NumSlices);
	Json += TEXT(",\"source_format\":\"") + JsonEscape(FormatName) + TEXT("\"");
	Json += FString::Printf(TEXT(",\"source_format_value\":%d"), static_cast<int32>(Format));
	Json += FString::Printf(TEXT(",\"num_mips\":%d,\"num_layers\":%d,\"num_blocks\":%d"), NumMips, NumLayers, NumBlocks);
	Json += FString::Printf(TEXT(",\"bytes_per_pixel\":%d,\"num_components\":%d,\"size_on_disk\":%lld"), BytesPerPixel, NumComponents, SizeOnDisk);
	Json += TEXT(",\"has_payload\":") + FString(bHasPayload ? TEXT("true") : TEXT("false"));

	const uint8* Data = nullptr;
	int64 MipSize = 0;
	if (bHasPayload && NumMips > 0 && SizeX > 0 && SizeY > 0)
	{
		MipSize = Source.CalcMipSize(0);
#if ENGINE_MAJOR_VERSION >= 5
		Data = Source.LockMipReadOnly(0);
#else
		Data = Source.LockMip(0);
#endif
	}
	Json += FString::Printf(TEXT(",\"mip0_bytes\":%lld"), MipSize);

	auto AppendByteStats = [&Json](const TCHAR* FieldName, const uint8* Bytes, int64 PixelCount, int32 Stride, int32 Offset, int32 FullValue)
	{
		if (!Bytes || PixelCount <= 0 || Stride <= 0)
		{
			Json += FString::Printf(TEXT(",\"%s\":{\"available\":false}"), FieldName);
			return;
		}
		int32 MinValue = 255;
		int32 MaxValue = 0;
		int64 ZeroCount = 0;
		int64 FullCount = 0;
		double Sum = 0.0;
		for (int64 Index = 0; Index < PixelCount; ++Index)
		{
			const int32 Value = Bytes[Index * Stride + Offset];
			MinValue = FMath::Min(MinValue, Value);
			MaxValue = FMath::Max(MaxValue, Value);
			ZeroCount += Value == 0 ? 1 : 0;
			FullCount += Value == FullValue ? 1 : 0;
			Sum += static_cast<double>(Value);
		}
		Json += FString::Printf(
			TEXT(",\"%s\":{\"available\":true,\"min\":%d,\"max\":%d,\"mean\":%.6f,\"zero_count\":%lld,\"full_count\":%lld,\"nonzero_count\":%lld,\"pixel_count\":%lld}"),
			FieldName, MinValue, MaxValue, Sum / static_cast<double>(PixelCount), ZeroCount, FullCount, PixelCount - ZeroCount, PixelCount);
	};

	const int64 PixelCount = SizeX * SizeY * FMath::Max<int32>(NumSlices, 1);
	if (Data && Format == TSF_BGRA8)
	{
		AppendByteStats(TEXT("alpha_stats"), Data, PixelCount, 4, 3, 255);
	}
	else
	{
		Json += TEXT(",\"alpha_stats\":{\"available\":false,\"reason\":\"alpha stats currently supported for TSF_BGRA8 source data\"}");
	}
	if (Data && Format == TSF_G8)
	{
		AppendByteStats(TEXT("value_stats"), Data, PixelCount, 1, 0, 255);
	}
	else
	{
		Json += TEXT(",\"value_stats\":{\"available\":false}");
	}

	if (Data)
	{
		Source.UnlockMip(0);
	}

	Json += TEXT("}");
	return Json;
}

FVector4 UCliAnythingBridgeLibrary::GetActiveViewportScreenBounds()
{
	FVector4 Bounds(0.0f, 0.0f, 0.0f, 0.0f);
	if (!FModuleManager::Get().IsModuleLoaded("LevelEditor")) return Bounds;
	FLevelEditorModule& LevelEditorModule = FModuleManager::GetModuleChecked<FLevelEditorModule>("LevelEditor");
	TSharedPtr<ILevelEditor> ActiveLevelEditor = LevelEditorModule.GetFirstLevelEditor();
	if (!ActiveLevelEditor.IsValid()) return Bounds;
	auto ActiveViewport = ActiveLevelEditor->GetActiveViewportInterface();
	if (!ActiveViewport.IsValid()) return Bounds;
	TSharedPtr<SViewport> ViewportWidget = ActiveViewport->GetViewportWidget().Pin();
	if (!ViewportWidget.IsValid()) return Bounds;
	FGeometry ViewportGeometry = ViewportWidget->GetCachedGeometry();
	FVector2D AbsolutePosition = ViewportGeometry.GetAbsolutePositionAtCoordinates(FVector2D(0.0f, 0.0f));
	FVector2D AbsoluteSize = ViewportGeometry.GetAbsoluteSize();
	Bounds.X = FMath::RoundToInt(AbsolutePosition.X);
	Bounds.Y = FMath::RoundToInt(AbsolutePosition.Y);
	Bounds.Z = FMath::RoundToInt(AbsoluteSize.X);
	Bounds.W = FMath::RoundToInt(AbsoluteSize.Y);
	return Bounds;
}

bool UCliAnythingBridgeLibrary::TakeActiveViewportScreenshot(const FString& OutputPath, bool bIncludeUI)
{
	if (OutputPath.IsEmpty() || !FModuleManager::Get().IsModuleLoaded("LevelEditor"))
	{
		return false;
	}

	FLevelEditorModule& LevelEditorModule = FModuleManager::GetModuleChecked<FLevelEditorModule>("LevelEditor");
	TSharedPtr<ILevelEditor> ActiveLevelEditor = LevelEditorModule.GetFirstLevelEditor();
	if (!ActiveLevelEditor.IsValid())
	{
		return false;
	}

	auto ActiveViewport = ActiveLevelEditor->GetActiveViewportInterface();
	if (!ActiveViewport.IsValid())
	{
		for (const auto& Viewport : ActiveLevelEditor->GetViewports())
		{
			if (Viewport.IsValid() && Viewport->GetSharedActiveViewport().IsValid())
			{
				ActiveViewport = Viewport;
				break;
			}
		}
		if (!ActiveViewport.IsValid())
		{
			return false;
		}
	}

	TSharedPtr<FSceneViewport> SceneViewport = ActiveViewport->GetSharedActiveViewport();
	if (!SceneViewport.IsValid())
	{
		return false;
	}

	SceneViewport->Draw();
	FlushRenderingCommands();

	FIntPoint Size;
	TArray<FColor> Pixels;
	if (bIncludeUI)
	{
		if (!FSlateApplication::IsInitialized())
		{
			return false;
		}
		TSharedPtr<SViewport> ViewportWidget = ActiveViewport->GetViewportWidget().Pin();
		if (!ViewportWidget.IsValid())
		{
			return false;
		}
		FIntVector ScreenshotSize;
		if (!FSlateApplication::Get().TakeScreenshot(ViewportWidget.ToSharedRef(), Pixels, ScreenshotSize))
		{
			return false;
		}
		Size = FIntPoint(ScreenshotSize.X, ScreenshotSize.Y);
	}
	else
	{
		Size = SceneViewport->GetRenderTargetTextureSizeXY();
		const FIntRect CaptureRect(0, 0, Size.X, Size.Y);
		Pixels.SetNum(CaptureRect.Area());
		if (!SceneViewport->ReadPixels(
			Pixels,
			FReadSurfaceDataFlags(RCM_UNorm, CubeFace_MAX),
			CaptureRect))
		{
			return false;
		}
	}
	if (Size.X <= 0 || Size.Y <= 0)
	{
		return false;
	}
	const int64 PixelCount = static_cast<int64>(Size.X) * Size.Y;
	if (Pixels.Num() != PixelCount)
	{
		return false;
	}
	for (int64 Index = 0; Index < PixelCount; ++Index)
	{
		Pixels[Index].A = 255;
	}

#if ENGINE_MAJOR_VERSION >= 5
	TArray64<uint8> CompressedPng;
	FImageUtils::PNGCompressImageArray(
		Size.X,
		Size.Y,
		TArrayView64<const FColor>(Pixels.GetData(), PixelCount),
		CompressedPng);
#else
	TArray<uint8> CompressedPng;
	FImageUtils::CompressImageArray(Size.X, Size.Y, Pixels, CompressedPng);
#endif
	if (CompressedPng.Num() == 0)
	{
		return false;
	}

	const FString Directory = FPaths::GetPath(OutputPath);
	if (!Directory.IsEmpty() && !IFileManager::Get().MakeDirectory(*Directory, true))
	{
		return false;
	}
	return FFileHelper::SaveArrayToFile(CompressedPng, *OutputPath);
}

extern TArray<FString> GCapturedEngineErrors;
extern FCriticalSection GCapturedEngineErrorsMutex;

TArray<FString> UCliAnythingBridgeLibrary::GetRecentEngineErrors(int32 Count)
{
	TArray<FString> Result;
	{
		FScopeLock Lock(&GCapturedEngineErrorsMutex);
		int32 NumLogs = GCapturedEngineErrors.Num();
		int32 StartIdx = FMath::Max(0, NumLogs - Count);
		for (int32 i = StartIdx; i < NumLogs; ++i) { Result.Add(GCapturedEngineErrors[i]); }
	}
	return Result;
}

FString UCliAnythingBridgeLibrary::GetPluginVersion()
{
	return TEXT("1.40");
}

FString UCliAnythingBridgeLibrary::ConnectMaterialOutput(UMaterial* Material, const FString& FromNode, const FString& FromOutputName, const FString& PropertyName)
{
	if (!Material) return JsonError(TEXT("Material is null"));
	TArray<FString> AvailableNodes;
	UMaterialExpression* FromExpression = FindMaterialExpression(Material, FromNode, &AvailableNodes);
	if (!FromExpression) return TEXT("{\"error\":\"Node not found: ") + JsonEscape(FromNode) + TEXT("\",\"available_nodes\":") + JsonStringArray(AvailableNodes) + TEXT("}");

	EMaterialProperty Property = MP_MAX;
	if (!ResolveMaterialProperty(PropertyName, Property))
	{
		return JsonError(TEXT("Unknown material property: ") + PropertyName);
	}
	Material->Modify();
	if (!UMaterialEditingLibrary::ConnectMaterialProperty(FromExpression, FromOutputName, Property))
	{
		return JsonError(TEXT("ConnectMaterialProperty returned false for: ") + PropertyName);
	}
	RecompileEditedMaterial(Material);

	return TEXT("{\"status\":\"ok\",\"action\":\"connect\",\"from\":\"")
		+ JsonEscape(FromNode)
		+ TEXT("\",\"to\":\"MaterialOutput.")
		+ JsonEscape(PropertyName)
		+ TEXT("\"}");
}

FString UCliAnythingBridgeLibrary::DisconnectMaterialOutput(UMaterial* Material, const FString& PropertyName)
{
	if (!Material)
	{
		return JsonError(TEXT("Material is null"));
	}

	EMaterialProperty Property = MP_MAX;
	if (!ResolveMaterialProperty(PropertyName, Property))
	{
		return JsonError(TEXT("Unknown material property: ") + PropertyName);
	}

	FExpressionInput* Input = Material->GetExpressionInputForProperty(Property);
	if (!Input)
	{
		return JsonError(TEXT("Material property input is unavailable: ") + PropertyName);
	}

	Material->Modify();
	Input->Expression = nullptr;
	Input->OutputIndex = 0;
	RecompileEditedMaterial(Material);

	return TEXT("{\"status\":\"ok\",\"action\":\"disconnect\",\"to\":\"MaterialOutput.")
		+ JsonEscape(PropertyName)
		+ TEXT("\"}");
}

FString UCliAnythingBridgeLibrary::RecompileMaterial(UMaterial* Material)
{
	if (!Material) return JsonError(TEXT("Material is null"));
	Material->Modify();
	RecompileEditedMaterial(Material);
	const TArray<FString> Errors = GetMaterialCompileErrors(Material);
	if (Errors.Num() > 0)
	{
		return TEXT("{\"status\":\"error\",\"action\":\"recompile\",\"material\":\"")
			+ JsonEscape(Material->GetPathName())
			+ TEXT("\",\"error\":\"Material compilation failed.\",\"compile_errors\":")
			+ JsonStringArray(Errors)
			+ TEXT("}");
	}
	return TEXT("{\"status\":\"ok\",\"action\":\"recompile\",\"material\":\"")
		+ JsonEscape(Material->GetPathName())
		+ TEXT("\"}");
}

FString UCliAnythingBridgeLibrary::GetConsoleVariableInfo(const FString& Name)
{
	IConsoleVariable* Var = IConsoleManager::Get().FindConsoleVariable(*Name);
	FString Json = TEXT("{\"name\":\"") + JsonEscape(Name) + TEXT("\"");
	if (!Var)
	{
		Json += TEXT(",\"exists\":false,\"value\":\"\"}");
		return Json;
	}

	Json += TEXT(",\"exists\":true");
	Json += TEXT(",\"value\":\"") + JsonEscape(Var->GetString()) + TEXT("\"");
	Json += FString::Printf(TEXT(",\"flags\":%d"), Var->GetFlags());
	Json += TEXT("}");
	return Json;
}

FString UCliAnythingBridgeLibrary::GetActiveShaderPlatform()
{
#if ENGINE_MAJOR_VERSION >= 5
	const EShaderPlatform ActivePlatform = GEditor
		? GEditor->GetActiveShaderPlatform()
		: GMaxRHIShaderPlatform;
	return FDataDrivenShaderPlatformInfo::GetName(ActivePlatform).ToString();
#else
	return LegacyShaderPlatformToShaderFormat(GMaxRHIShaderPlatform).ToString();
#endif
}

FString UCliAnythingBridgeLibrary::RecompileMaterialShadersForDump(UMaterialInterface* Material)
{
	if (!Material)
	{
		return JsonError(TEXT("Material is null"));
	}

	UPackage* Package = Material->GetOutermost();
	const bool bDirtyBefore = Package && Package->IsDirty();

	Material->PreEditChange(nullptr);
	Material->PostEditChange();

	const bool bDirtyAfterRecompile = Package && Package->IsDirty();
	if (Package && bDirtyAfterRecompile != bDirtyBefore)
	{
		Package->SetDirtyFlag(bDirtyBefore);
	}
	const bool bDirtyRestored = !Package || Package->IsDirty() == bDirtyBefore;

	UMaterial* BaseMaterial = Material->GetBaseMaterial();
	const FString ShaderMapMaterial = BaseMaterial ? BaseMaterial->GetPathName() : Material->GetPathName();
	FString ShaderDumpName = BaseMaterial ? BaseMaterial->GetName() : Material->GetName();
	FString ShaderDebugGroup;
#if ENGINE_MAJOR_VERSION >= 5
	const EShaderPlatform ActivePlatform = GEditor
		? GEditor->GetActiveShaderPlatform()
		: GMaxRHIShaderPlatform;
	FMaterialResource* MaterialResource = Material->GetMaterialResource(ActivePlatform);
#else
	FMaterialResource* MaterialResource = Material->GetMaterialResource(GMaxRHIFeatureLevel);
#endif
	if (MaterialResource)
	{
		ShaderDumpName = MaterialResource->GetFriendlyName();
#if ENGINE_MAJOR_VERSION >= 5
		ShaderDebugGroup = MaterialResource->GetDebugGroupName();
#endif
	}

	FString Json = TEXT("{\"status\":\"ok\",\"active_platform\":\"")
		+ JsonEscape(GetActiveShaderPlatform()) + TEXT("\"");
	Json += TEXT(",\"package_dirty_before\":") + FString(bDirtyBefore ? TEXT("true") : TEXT("false"));
	Json += TEXT(",\"package_dirty_after_recompile\":") + FString(bDirtyAfterRecompile ? TEXT("true") : TEXT("false"));
	Json += TEXT(",\"package_dirty_restored\":") + FString(bDirtyRestored ? TEXT("true") : TEXT("false"));
	Json += TEXT(",\"shader_map_material\":\"") + JsonEscape(ShaderMapMaterial) + TEXT("\"");
	Json += TEXT(",\"shader_dump_name\":\"") + JsonEscape(ShaderDumpName) + TEXT("\"");
	Json += TEXT(",\"shader_debug_group\":\"") + JsonEscape(ShaderDebugGroup) + TEXT("\"");
	Json += TEXT("}");
	return Json;
}

TArray<FString> UCliAnythingBridgeLibrary::GetMaterialHLSLCode(UMaterialInterface* Material, const FString& OutputPath)
{
	TArray<FString> Result;
	if (!Material || OutputPath.IsEmpty()) return Result;
#if ENGINE_MAJOR_VERSION >= 5
	const EShaderPlatform Platform = GEditor ? GEditor->GetActiveShaderPlatform() : GMaxRHIShaderPlatform;
	FMaterialResource* Resource = Material->GetMaterialResource(Platform);
#else
	FMaterialResource* Resource = Material->GetMaterialResource(GMaxRHIFeatureLevel);
#endif
	if (!Resource) return Result;
	FString Source;
	if (!Resource->GetMaterialExpressionSource(Source)) return Result;
	if (FFileHelper::SaveStringToFile(Source, *OutputPath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
	{
		Result.Add(OutputPath);
	}
	return Result;
}

TArray<FString> UCliAnythingBridgeLibrary::GetMaterialShaderSource(UMaterialInterface* Material, const FString& OutputDir)
{
	TArray<FString> Result;
	if (!Material || OutputDir.IsEmpty()) return Result;
	UMaterial* BaseMat = Material->GetMaterial();
	if (!BaseMat) return Result;
#if ENGINE_MAJOR_VERSION >= 5
	const EShaderPlatform Platform = GEditor ? GEditor->GetActiveShaderPlatform() : GMaxRHIShaderPlatform;
#else
	const EShaderPlatform Platform = GMaxRHIShaderPlatform;
#endif
	if (!GEngine || !GEngine->HandleRecompileShadersCommand(TEXT("Changed"), *GLog))
	{
		return Result;
	}

	FMaterialResourceExtractSource* ExtractResource = new FMaterialResourceExtractSource();
	UMaterialInstance* Instance = Cast<UMaterialInstance>(Material);
#if ENGINE_MAJOR_VERSION >= 5
	ExtractResource->SetMaterial(BaseMat, Instance, Platform, EMaterialQualityLevel::High);
#else
	ExtractResource->SetMaterial(BaseMat, Instance, GMaxRHIFeatureLevel, EMaterialQualityLevel::High);
#endif
	BaseMat->UpdateCachedExpressionData();
#if ENGINE_MAJOR_VERSION >= 5
	ExtractResource->CacheShaders(EMaterialShaderPrecompileMode::Default);
#else
	ExtractResource->CacheShaders(Platform);
#endif
	GShaderCompilingManager->FinishAllCompilation();

	const bool bFinished = ExtractResource->IsCompilationFinished();
	const FMaterialShaderMap* ShaderMap = ExtractResource->GetGameThreadShaderMap();
	if (bFinished && ShaderMap)
	{
		TMap<FShaderId, TShaderRef<FShader>> ShaderList;
		ShaderMap->GetShaderList(ShaderList);
		for (const auto& Pair : ShaderList)
		{
			const FShaderId& ShaderId = Pair.Key;
			const TShaderRef<FShader>& ShaderRef = Pair.Value;
#if ENGINE_MAJOR_VERSION >= 5
			const FMemoryImageString* Source = ShaderMap->GetShaderSource(
				ShaderRef.GetVertexFactoryType(), ShaderRef.GetType(), ShaderId.PermutationId);
#else
			const FMemoryImageString* Source = ShaderMap->GetShaderSource(ShaderRef.GetType()->GetFName());
#endif
			if (!Source || Source->Len() == 0) continue;

			FString ShaderName = ShaderRef.GetType()->GetName();
			if (ShaderRef.GetVertexFactoryType())
			{
				ShaderName += TEXT("_");
				ShaderName += ShaderRef.GetVertexFactoryType()->GetName();
			}
			if (ShaderId.PermutationId != 0)
			{
				ShaderName += FString::Printf(TEXT("_Perm%d"), ShaderId.PermutationId);
			}
			ShaderName.ReplaceCharInline(TEXT('<'), TEXT('_'));
			ShaderName.ReplaceCharInline(TEXT('>'), TEXT('_'));
			ShaderName.ReplaceCharInline(TEXT(':'), TEXT('_'));
			ShaderName.ReplaceCharInline(TEXT('"'), TEXT('_'));
			ShaderName.ReplaceCharInline(TEXT('/'), TEXT('_'));
			ShaderName.ReplaceCharInline(TEXT('\\'), TEXT('_'));
			ShaderName.ReplaceCharInline(TEXT('|'), TEXT('_'));
			ShaderName.ReplaceCharInline(TEXT('?'), TEXT('_'));
			ShaderName.ReplaceCharInline(TEXT('*'), TEXT('_'));

			FString FilePath = FPaths::Combine(OutputDir, ShaderName + TEXT(".usf"));
			IFileManager::Get().MakeDirectory(*OutputDir, true);
			if (FFileHelper::SaveStringToFile(FString(**Source), *FilePath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
			{
				int32 LineCount = 0;
				for (int32 i = 0; i < Source->Len(); ++i) { if ((**Source)[i] == TEXT('\n')) LineCount++; }
				Result.Add(FString::Printf(TEXT("%s\t%s\t%d"), *ShaderName, *FilePath, LineCount));
			}
		}
	}

#if ENGINE_MAJOR_VERSION >= 5
	TArray<TRefCountPtr<FMaterial>> MaterialsToDelete;
	if (ExtractResource->PrepareDestroy_GameThread())
	{
		MaterialsToDelete.Add(ExtractResource);
		FMaterial::DeleteMaterialsOnRenderThread(MaterialsToDelete);
	}
	else
	{
		delete ExtractResource;
	}
#else
	delete ExtractResource;
#endif
	return Result;
}

// Escape a string for safe embedding in a JSON value ("..." context).
// UE's ReplaceCharWithEscapedChar() is designed for C++ string literals, not JSON,
// and produces invalid sequences like \\' from UE metadata tooltips.
static FString JsonEscape(const FString& Input)
{
	FString Result;
	Result.Reserve(Input.Len() + 16);
	for (const TCHAR Ch : Input)
	{
		switch (Ch)
		{
		case TEXT('"'):  Result += TEXT("\\\""); break;
		case TEXT('\\'): Result += TEXT("\\\\"); break;
		case TEXT('\n'): Result += TEXT("\\n");  break;
		case TEXT('\r'): Result += TEXT("\\r");  break;
		case TEXT('\t'): Result += TEXT("\\t");  break;
		default:        Result += Ch;            break;
		}
	}
	return Result;
}

static FString JsonError(const FString& Message)
{
	return TEXT("{\"error\":\"") + JsonEscape(Message) + TEXT("\"}");
}

static FString JsonStringArray(const TArray<FString>& Values)
{
	FString Result = TEXT("[");
	for (int32 Index = 0; Index < Values.Num(); ++Index)
	{
		if (Index > 0)
		{
			Result += TEXT(",");
		}
		Result += TEXT("\"") + JsonEscape(Values[Index]) + TEXT("\"");
	}
	Result += TEXT("]");
	return Result;
}

static UClass* FindWidgetClassByName(const FString& ClassName)
{
	FString Name = ClassName;
	Name.TrimStartAndEndInline();
	Name.RemoveFromStart(TEXT("unreal."));
	if (Name.IsEmpty()) return nullptr;

	if (Name.StartsWith(TEXT("/Script/")) || Name.Contains(TEXT(".")))
	{
		if (UClass* LoadedClass = LoadClass<UWidget>(nullptr, *Name))
		{
			return LoadedClass->IsChildOf(UWidget::StaticClass()) ? LoadedClass : nullptr;
		}
	}

	for (TObjectIterator<UClass> It; It; ++It)
	{
		UClass* Candidate = *It;
		if (!Candidate || !Candidate->IsChildOf(UWidget::StaticClass())) continue;
		if (Candidate->GetName() == Name || Candidate->GetPathName() == Name)
		{
			return Candidate;
		}
	}
	return nullptr;
}

static FString WidgetJson(UWidget* Widget, UWidgetBlueprint* Blueprint)
{
	if (!Widget) return TEXT("{}");

	FString Json = TEXT("{\"name\":\"") + JsonEscape(Widget->GetName()) + TEXT("\"");
	Json += TEXT(",\"class\":\"") + JsonEscape(Widget->GetClass()->GetName()) + TEXT("\"");
	Json += TEXT(",\"path\":\"") + JsonEscape(Widget->GetPathName()) + TEXT("\"");
	Json += TEXT(",\"is_variable\":");
	Json += Widget->bIsVariable ? TEXT("true") : TEXT("false");

	const bool bIsRoot = Blueprint && Blueprint->WidgetTree && Blueprint->WidgetTree->RootWidget == Widget;
	Json += TEXT(",\"is_root\":");
	Json += bIsRoot ? TEXT("true") : TEXT("false");

	int32 ChildIndex = INDEX_NONE;
	if (UPanelWidget* Parent = UWidgetTree::FindWidgetParent(Widget, ChildIndex))
	{
		Json += TEXT(",\"parent\":\"") + JsonEscape(Parent->GetName()) + TEXT("\"");
		Json += FString::Printf(TEXT(",\"child_index\":%d"), ChildIndex);
	}

	if (UCanvasPanelSlot* CanvasSlot = Cast<UCanvasPanelSlot>(Widget->Slot))
	{
		const FVector2D Position = CanvasSlot->GetPosition();
		const FVector2D Size = CanvasSlot->GetSize();
		Json += FString::Printf(
			TEXT(",\"slot\":{\"type\":\"CanvasPanelSlot\",\"position\":[%.6g,%.6g],\"size\":[%.6g,%.6g],\"z_order\":%d}"),
			Position.X, Position.Y, Size.X, Size.Y, CanvasSlot->GetZOrder());
	}

	if (UTextBlock* TextBlock = Cast<UTextBlock>(Widget))
	{
		Json += TEXT(",\"text\":\"") + JsonEscape(TextBlock->GetText().ToString()) + TEXT("\"");
	}

	if (UImage* Image = Cast<UImage>(Widget))
	{
#if ENGINE_MAJOR_VERSION >= 5
		const FSlateBrush& Brush = Image->GetBrush();
#else
		const FSlateBrush& Brush = Image->Brush;
#endif
		UObject* Resource = Brush.GetResourceObject();
		const FVector2D BrushSize = Brush.GetImageSize();
		Json += TEXT(",\"brush\":{");
		Json += TEXT("\"resource\":");
		if (Resource)
		{
			Json += TEXT("\"") + JsonEscape(Resource->GetPathName()) + TEXT("\"");
			Json += TEXT(",\"resource_class\":\"") + JsonEscape(Resource->GetClass()->GetName()) + TEXT("\"");
		}
		else
		{
			Json += TEXT("null");
		}
		Json += FString::Printf(TEXT(",\"image_size\":[%.6g,%.6g]"), BrushSize.X, BrushSize.Y);
		Json += FString::Printf(TEXT(",\"draw_as\":%d"), static_cast<int32>(Brush.GetDrawType()));
		Json += TEXT("}");
	}

	if (UPanelWidget* Panel = Cast<UPanelWidget>(Widget))
	{
		Json += TEXT(",\"children\":[");
		for (int32 Index = 0; Index < Panel->GetChildrenCount(); ++Index)
		{
			if (Index > 0) Json += TEXT(",");
			if (UWidget* Child = Panel->GetChildAt(Index))
			{
				Json += TEXT("{\"name\":\"") + JsonEscape(Child->GetName()) + TEXT("\"");
				Json += TEXT(",\"class\":\"") + JsonEscape(Child->GetClass()->GetName()) + TEXT("\"}");
			}
			else
			{
				Json += TEXT("{}");
			}
		}
		Json += TEXT("]");
	}

	Json += TEXT("}");
	return Json;
}

FString UCliAnythingBridgeLibrary::SetWidgetBlueprintRoot(UWidgetBlueprint* Blueprint, const FString& RootWidgetClassName, const FString& RootWidgetName, bool bIsVariable)
{
	if (!Blueprint) return JsonError(TEXT("WidgetBlueprint is null."));
	if (!Blueprint->WidgetTree) return JsonError(TEXT("WidgetBlueprint has no WidgetTree."));

	UClass* RootClass = FindWidgetClassByName(RootWidgetClassName.IsEmpty() ? TEXT("CanvasPanel") : RootWidgetClassName);
	if (!RootClass) return JsonError(TEXT("Root widget class not found or not a UWidget: ") + RootWidgetClassName);

	const FName RootName = RootWidgetName.IsEmpty() ? FName(TEXT("RootCanvas")) : FName(*RootWidgetName);
	UWidget* Root = Blueprint->WidgetTree->RootWidget;
	if (Root)
	{
		if (Root->GetClass() != RootClass)
		{
			return JsonError(
				TEXT("WidgetBlueprint already has a root widget of class ")
				+ Root->GetClass()->GetName()
				+ TEXT("; requested ")
				+ RootClass->GetName()
			);
		}

		UWidget* NameOwner = Blueprint->WidgetTree->FindWidget(RootName);
		if (NameOwner && NameOwner != Root)
		{
			return JsonError(TEXT("Widget name already exists: ") + RootName.ToString());
		}

		Root->Modify();
		if (Root->GetFName() != RootName && !Root->Rename(*RootName.ToString(), Blueprint->WidgetTree))
		{
			return JsonError(TEXT("Failed to rename existing root widget to: ") + RootName.ToString());
		}
		Root->bIsVariable = bIsVariable;
		FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
		Blueprint->MarkPackageDirty();

		FString ExistingJson = TEXT("{\"status\":\"ok\",\"action\":\"set_root\",\"root\":");
		ExistingJson += WidgetJson(Root, Blueprint);
		ExistingJson += TEXT("}");
		return ExistingJson;
	}

	if (Blueprint->WidgetTree->FindWidget(RootName))
	{
		return JsonError(TEXT("Widget name already exists: ") + RootName.ToString());
	}

	Root = Blueprint->WidgetTree->ConstructWidget<UWidget>(RootClass, RootName);
	if (!Root) return JsonError(TEXT("Failed to construct root widget."));

	Root->bIsVariable = bIsVariable;
	Blueprint->WidgetTree->RootWidget = Root;
	if (bIsVariable)
	{
#if ENGINE_MAJOR_VERSION >= 5
		Blueprint->OnVariableAdded(Root->GetFName());
#endif
	}
	FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
	Blueprint->MarkPackageDirty();

	FString Json = TEXT("{\"status\":\"ok\",\"action\":\"set_root\",\"root\":");
	Json += WidgetJson(Root, Blueprint);
	Json += TEXT("}");
	return Json;
}

FString UCliAnythingBridgeLibrary::AddWidgetToCanvas(UWidgetBlueprint* Blueprint, const FString& WidgetClassName, const FString& WidgetName, const FString& ParentWidgetName, bool bIsVariable, float X, float Y, float Width, float Height, int32 ZOrder, const FString& Text)
{
	if (!Blueprint) return JsonError(TEXT("WidgetBlueprint is null."));
	if (!Blueprint->WidgetTree) return JsonError(TEXT("WidgetBlueprint has no WidgetTree."));
	if (!Blueprint->WidgetTree->RootWidget) return JsonError(TEXT("WidgetBlueprint has no root widget."));

	UClass* WidgetClass = FindWidgetClassByName(WidgetClassName);
	if (!WidgetClass) return JsonError(TEXT("Widget class not found or not a UWidget: ") + WidgetClassName);
	if (WidgetName.IsEmpty()) return JsonError(TEXT("Widget name is required."));

	const FName NewWidgetName(*WidgetName);
	if (Blueprint->WidgetTree->FindWidget(NewWidgetName))
	{
		return JsonError(TEXT("Widget name already exists: ") + WidgetName);
	}

	UWidget* ParentWidget = nullptr;
	if (ParentWidgetName.IsEmpty())
	{
		ParentWidget = Blueprint->WidgetTree->RootWidget;
	}
	else
	{
		ParentWidget = Blueprint->WidgetTree->FindWidget(FName(*ParentWidgetName));
	}

	UCanvasPanel* Canvas = Cast<UCanvasPanel>(ParentWidget);
	if (!Canvas)
	{
		const FString ParentLabel = ParentWidget ? ParentWidget->GetName() : ParentWidgetName;
		return JsonError(TEXT("Parent widget is not a CanvasPanel: ") + ParentLabel);
	}

	UWidget* Child = Blueprint->WidgetTree->ConstructWidget<UWidget>(WidgetClass, NewWidgetName);
	if (!Child) return JsonError(TEXT("Failed to construct widget: ") + WidgetName);

	Child->bIsVariable = bIsVariable;
	if (UTextBlock* TextBlock = Cast<UTextBlock>(Child))
	{
		if (!Text.IsEmpty())
		{
			TextBlock->SetText(FText::FromString(Text));
		}
	}

	UCanvasPanelSlot* Slot = Canvas->AddChildToCanvas(Child);
	if (!Slot) return JsonError(TEXT("Failed to add widget to CanvasPanel."));
	Slot->SetPosition(FVector2D(X, Y));
	if (Width >= 0.0f && Height >= 0.0f)
	{
		Slot->SetSize(FVector2D(Width, Height));
	}
	Slot->SetZOrder(ZOrder);

	if (bIsVariable)
	{
#if ENGINE_MAJOR_VERSION >= 5
		Blueprint->OnVariableAdded(Child->GetFName());
#endif
	}
	FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
	Blueprint->MarkPackageDirty();

	FString Json = TEXT("{\"status\":\"ok\",\"action\":\"add_widget\",\"parent\":\"");
	Json += JsonEscape(Canvas->GetName());
	Json += TEXT("\",\"widget\":");
	Json += WidgetJson(Child, Blueprint);
	Json += TEXT("}");
	return Json;
}

FString UCliAnythingBridgeLibrary::SetWidgetImageProperties(UWidgetBlueprint* Blueprint, const FString& WidgetName, UObject* ResourceObject, bool bSetResource, bool bSetPosition, float X, float Y, bool bSetSize, float Width, float Height, bool bSetZOrder, int32 ZOrder, bool bSetBrushImageSize, float ImageWidth, float ImageHeight)
{
	if (!Blueprint) return JsonError(TEXT("WidgetBlueprint is null."));
	if (!Blueprint->WidgetTree) return JsonError(TEXT("WidgetBlueprint has no WidgetTree."));
	if (WidgetName.IsEmpty()) return JsonError(TEXT("Widget name is required."));

	UWidget* Widget = Blueprint->WidgetTree->FindWidget(FName(*WidgetName));
	if (!Widget) return JsonError(TEXT("Widget not found: ") + WidgetName);

	UImage* Image = Cast<UImage>(Widget);
	if (!Image) return JsonError(TEXT("Widget is not an Image: ") + WidgetName);

	Image->Modify();
	if (bSetResource)
	{
		if (!ResourceObject) return JsonError(TEXT("Brush resource is null."));
		Image->SetBrushResourceObject(ResourceObject);
	}

	if (bSetBrushImageSize)
	{
		if (ImageWidth < 0.0f || ImageHeight < 0.0f)
		{
			return JsonError(TEXT("Brush ImageSize must be non-negative."));
		}
#if ENGINE_MAJOR_VERSION >= 5
		FSlateBrush Brush = Image->GetBrush();
#else
		FSlateBrush Brush = Image->Brush;
#endif
		Brush.ImageSize = FVector2D(ImageWidth, ImageHeight);
		Image->SetBrush(Brush);
	}

	if (bSetPosition || bSetSize || bSetZOrder)
	{
		UCanvasPanelSlot* CanvasSlot = Cast<UCanvasPanelSlot>(Image->Slot);
		if (!CanvasSlot) return JsonError(TEXT("Image widget is not in a CanvasPanelSlot: ") + WidgetName);
		CanvasSlot->Modify();
		if (bSetPosition)
		{
			CanvasSlot->SetPosition(FVector2D(X, Y));
		}
		if (bSetSize)
		{
			CanvasSlot->SetSize(FVector2D(Width, Height));
		}
		if (bSetZOrder)
		{
			CanvasSlot->SetZOrder(ZOrder);
		}
	}

	FBlueprintEditorUtils::MarkBlueprintAsModified(Blueprint);
	Blueprint->MarkPackageDirty();

	FString Json = TEXT("{\"status\":\"ok\",\"action\":\"set_image\",\"widget\":");
	Json += WidgetJson(Image, Blueprint);
	Json += TEXT("}");
	return Json;
}

FString UCliAnythingBridgeLibrary::GetWidgetBlueprintTree(UWidgetBlueprint* Blueprint)
{
	if (!Blueprint) return JsonError(TEXT("WidgetBlueprint is null."));
	if (!Blueprint->WidgetTree) return JsonError(TEXT("WidgetBlueprint has no WidgetTree."));

	FString Json = TEXT("{\"status\":\"ok\",\"widget\":\"") + JsonEscape(Blueprint->GetPathName()) + TEXT("\"");
	if (Blueprint->WidgetTree->RootWidget)
	{
		Json += TEXT(",\"root\":");
		Json += WidgetJson(Blueprint->WidgetTree->RootWidget, Blueprint);
	}
	else
	{
		Json += TEXT(",\"root\":null");
	}

	TArray<UWidget*> Widgets;
	Blueprint->WidgetTree->GetAllWidgets(Widgets);
	Json += TEXT(",\"widgets\":[");
	for (int32 Index = 0; Index < Widgets.Num(); ++Index)
	{
		if (Index > 0) Json += TEXT(",");
		Json += WidgetJson(Widgets[Index], Blueprint);
	}
	Json += TEXT("]}");
	return Json;
}

FString UCliAnythingBridgeLibrary::GetClassInfo(const FString& ClassName, bool bIncludeInherited)
{
	// Find the UClass by name
	UClass* FoundClass = nullptr;
	for (TObjectIterator<UClass> It; It; ++It)
	{
		if (It->GetName() == ClassName)
		{
			FoundClass = *It;
			break;
		}
	}

	if (!FoundClass) return TEXT("{}");

	// Build JSON using TFieldIterator (same system the Details panel uses)
	FString Json = TEXT("{");
	Json += TEXT("\"class\":\"") + JsonEscape(FoundClass->GetName()) + TEXT("\"");

	// --- Properties: only those with EditAnywhere/VisibleAnywhere (like Details panel) ---
	Json += TEXT(",\"properties\":[");
	bool bFirst = true;

	EFieldIteratorFlags::SuperClassFlags SuperFlag = bIncludeInherited
		? EFieldIteratorFlags::IncludeSuper : EFieldIteratorFlags::ExcludeSuper;

	for (TFieldIterator<FProperty> It(FoundClass, SuperFlag, EFieldIteratorFlags::IncludeDeprecated); It; ++It)
	{
		const FProperty* Prop = *It;
		if (!Prop) continue;

		// Expose all reflected properties (no filter) — some types like
		// FExpressionInput have bare UPROPERTY() without Edit/Visible flags
		// but are still essential for understanding material expression inputs.
		const EPropertyFlags Flags = Prop->PropertyFlags;

		if (!bFirst) Json += TEXT(",");
		bFirst = false;

		Json += TEXT("{\"name\":\"") + JsonEscape(Prop->GetName()) + TEXT("\"");
		Json += TEXT(",\"type\":\"") + JsonEscape(Prop->GetCPPType()) + TEXT("\"");
		Json += TEXT(",\"owner\":\"") + JsonEscape(Prop->GetOwnerClass()->GetName()) + TEXT("\"");

		FString Category = Prop->GetMetaData(TEXT("Category"));
		if (!Category.IsEmpty())
			Json += TEXT(",\"category\":\"") + JsonEscape(Category) + TEXT("\"");

		FString Tooltip = Prop->GetMetaData(TEXT("Tooltip"));
		if (!Tooltip.IsEmpty())
		{
			Tooltip.ReplaceInline(TEXT("\r\n"), TEXT(" "));
			Tooltip.ReplaceInline(TEXT("\n"), TEXT(" "));
			Json += TEXT(",\"tooltip\":\"") + JsonEscape(Tooltip) + TEXT("\"");
		}

		Json += TEXT(",\"read\":");
		Json += (Flags & CPF_BlueprintVisible) ? TEXT("true") : TEXT("false");
		Json += TEXT(",\"write\":");
		Json += (Flags & CPF_Edit) ? TEXT("true") : TEXT("false");

		Json += TEXT("}");
	}
	Json += TEXT("]");

	// --- Functions: UFUNCTION only, skip internal/deprecated ---
	Json += TEXT(",\"functions\":[");
	bFirst = true;

	for (TFieldIterator<UFunction> It(FoundClass, SuperFlag, EFieldIteratorFlags::IncludeDeprecated); It; ++It)
	{
		const UFunction* Func = *It;
		if (!Func) continue;
		// Skip deprecated and Blueprint-internal functions
		if (Func->HasMetaData(TEXT("DeprecatedFunction")) ||
			Func->HasMetaData(TEXT("BlueprintInternalUseOnly"))) continue;

		if (!bFirst) Json += TEXT(",");
		bFirst = false;

		Json += TEXT("{\"name\":\"") + JsonEscape(Func->GetName()) + TEXT("\"");
		Json += TEXT(",\"owner\":\"") + JsonEscape(Func->GetOwnerClass()->GetName()) + TEXT("\"");

		FString FuncTooltip = Func->GetMetaData(TEXT("Tooltip"));
		if (!FuncTooltip.IsEmpty())
		{
			FuncTooltip.ReplaceInline(TEXT("\r\n"), TEXT(" "));
			FuncTooltip.ReplaceInline(TEXT("\n"), TEXT(" "));
			Json += TEXT(",\"tooltip\":\"") + JsonEscape(FuncTooltip) + TEXT("\"");
		}

		FProperty* ReturnProp = Func->GetReturnProperty();
		if (ReturnProp)
			Json += TEXT(",\"return_type\":\"") + JsonEscape(ReturnProp->GetCPPType()) + TEXT("\"");

		Json += TEXT(",\"params\":[");
		bool bFirstParam = true;
		for (TFieldIterator<FProperty> ParamIt(Func); ParamIt; ++ParamIt)
		{
			const FProperty* Param = *ParamIt;
			if (Param == ReturnProp) continue;
			if (Param->PropertyFlags & CPF_OutParm && !(Param->PropertyFlags & CPF_ConstParm)) continue;

			if (!bFirstParam) Json += TEXT(",");
			bFirstParam = false;
			Json += TEXT("{\"name\":\"") + JsonEscape(Param->GetName()) + TEXT("\"");
			Json += TEXT(",\"type\":\"") + JsonEscape(Param->GetCPPType()) + TEXT("\"");
			Json += TEXT("}");
		}
		Json += TEXT("]");

		Json += TEXT("}");
	}
	Json += TEXT("]");

	Json += TEXT("}");
	return Json;
}

FString UCliAnythingBridgeLibrary::GetStructInfo(UScriptStruct* Struct, bool bIncludeInherited)
{
	if (!Struct) return TEXT("{}");

	EFieldIteratorFlags::SuperClassFlags SuperFlag = bIncludeInherited
		? EFieldIteratorFlags::IncludeSuper : EFieldIteratorFlags::ExcludeSuper;

	FString Json = TEXT("{");
	Json += TEXT("\"struct\":\"") + JsonEscape(Struct->GetName()) + TEXT("\"");
	Json += TEXT(",\"struct_path\":\"") + JsonEscape(Struct->GetPathName()) + TEXT("\"");
	Json += TEXT(",\"properties\":[");

	bool bFirst = true;
	for (TFieldIterator<FProperty> It(Struct, SuperFlag, EFieldIteratorFlags::IncludeDeprecated); It; ++It)
	{
		const FProperty* Prop = *It;
		if (!Prop) continue;

		if (!bFirst) Json += TEXT(",");
		bFirst = false;

		Json += TEXT("{\"name\":\"") + JsonEscape(Prop->GetName()) + TEXT("\"");
		Json += TEXT(",\"type\":\"") + JsonEscape(Prop->GetCPPType()) + TEXT("\"");
		if (const UStruct* OwnerStruct = Prop->GetOwnerStruct())
		{
			Json += TEXT(",\"owner\":\"") + JsonEscape(OwnerStruct->GetName()) + TEXT("\"");
		}

		FString Category = Prop->GetMetaData(TEXT("Category"));
		if (!Category.IsEmpty())
		{
			Json += TEXT(",\"category\":\"") + JsonEscape(Category) + TEXT("\"");
		}

		FString Tooltip = Prop->GetMetaData(TEXT("Tooltip"));
		if (!Tooltip.IsEmpty())
		{
			Tooltip.ReplaceInline(TEXT("\r\n"), TEXT(" "));
			Tooltip.ReplaceInline(TEXT("\n"), TEXT(" "));
			Json += TEXT(",\"tooltip\":\"") + JsonEscape(Tooltip) + TEXT("\"");
		}

		const EPropertyFlags Flags = Prop->PropertyFlags;
		Json += TEXT(",\"read\":");
		Json += (Flags & CPF_BlueprintVisible) ? TEXT("true") : TEXT("false");
		Json += TEXT(",\"write\":");
		Json += (Flags & CPF_Edit) ? TEXT("true") : TEXT("false");
		Json += TEXT("}");
	}

	Json += TEXT("],\"functions\":[]}");
	return Json;
}

FString UCliAnythingBridgeLibrary::GetActorComponentTree(AActor* Actor, bool bIncludeVisualization)
{
	if (!Actor) return TEXT("[]");

	TArray<UActorComponent*> Components;
	Actor->GetComponents(Components);

	USceneComponent* RootComp = Actor->GetRootComponent();

	FString Json = TEXT("[");
	bool bFirst = true;
	for (UActorComponent* Comp : Components)
	{
		if (!Comp) continue;

		// Match the SCS Components tree in the Details panel: skip editor-only
		// visualization components (arrow gizmos, billboard icons, debug text, ...).
		if (!bIncludeVisualization && Comp->IsVisualizationComponent()) continue;

		if (!bFirst) Json += TEXT(",");
		bFirst = false;

		// Attach parent (scene components only); empty string for non-scene components
		FString ParentName;
		if (USceneComponent* SceneComp = Cast<USceneComponent>(Comp))
		{
			if (USceneComponent* Parent = SceneComp->GetAttachParent())
			{
				ParentName = Parent->GetName();
			}
		}

		const bool bIsRoot   = (Comp == RootComp);
		// RF_DefaultSubObject is set for components created via CreateDefaultSubobject
		// in the C++ constructor — i.e., "native" components. BP-added and instance
		// components lack this flag.
		const bool bIsNative = Comp->HasAnyFlags(RF_DefaultSubObject);

		Json += TEXT("{\"name\":\"") + JsonEscape(Comp->GetName()) + TEXT("\"");
		Json += TEXT(",\"class\":\"") + JsonEscape(Comp->GetClass()->GetName()) + TEXT("\"");
		Json += TEXT(",\"path\":\"") + JsonEscape(Comp->GetPathName()) + TEXT("\"");
		Json += TEXT(",\"is_root\":");
		Json += bIsRoot ? TEXT("true") : TEXT("false");
		Json += TEXT(",\"is_native\":");
		Json += bIsNative ? TEXT("true") : TEXT("false");
		if (!ParentName.IsEmpty())
		{
			Json += TEXT(",\"parent\":\"") + JsonEscape(ParentName) + TEXT("\"");
		}
		Json += TEXT("}");
	}
	Json += TEXT("]");
	return Json;
}

FString UCliAnythingBridgeLibrary::GetStaticMeshComponentLODProperty(
	UStaticMeshComponent* Component,
	int32 LODIndex,
	const FString& PropertyName)
{
	if (!Component) return JsonError(TEXT("StaticMeshComponent is null."));
	if (!Component->LODData.IsValidIndex(LODIndex))
	{
		return FString::Printf(
			TEXT("{\"error\":\"LODData index out of range: %d\",\"lod_index\":%d,\"lod_count\":%d}"),
			LODIndex,
			LODIndex,
			Component->LODData.Num());
	}

	const FStaticMeshComponentLODInfo& LODInfo = Component->LODData[LODIndex];
	FString ValueJson;
	if (PropertyName.Equals(TEXT("PaintedVertices"), ESearchCase::IgnoreCase))
	{
		ValueJson = TEXT("[");
		for (int32 Index = 0; Index < LODInfo.PaintedVertices.Num(); ++Index)
		{
			if (Index > 0) ValueJson += TEXT(",");
			const FPaintedVertex& Vertex = LODInfo.PaintedVertices[Index];
			ValueJson += FString::Printf(
				TEXT("{\"position\":{\"x\":%s,\"y\":%s,\"z\":%s},")
				TEXT("\"normal\":{\"x\":%s,\"y\":%s,\"z\":%s,\"w\":%s},")
				TEXT("\"color\":{\"r\":%d,\"g\":%d,\"b\":%d,\"a\":%d}}"),
				*FString::SanitizeFloat(Vertex.Position.X),
				*FString::SanitizeFloat(Vertex.Position.Y),
				*FString::SanitizeFloat(Vertex.Position.Z),
				*FString::SanitizeFloat(Vertex.Normal.X),
				*FString::SanitizeFloat(Vertex.Normal.Y),
				*FString::SanitizeFloat(Vertex.Normal.Z),
				*FString::SanitizeFloat(Vertex.Normal.W),
				static_cast<int32>(Vertex.Color.R),
				static_cast<int32>(Vertex.Color.G),
				static_cast<int32>(Vertex.Color.B),
				static_cast<int32>(Vertex.Color.A));
		}
		ValueJson += TEXT("]");
	}
	else if (PropertyName.Equals(TEXT("OverrideVertexColors"), ESearchCase::IgnoreCase))
	{
		if (!LODInfo.OverrideVertexColors)
		{
			ValueJson = TEXT("null");
		}
		else
		{
			const FColorVertexBuffer& Colors = *LODInfo.OverrideVertexColors;
			ValueJson = FString::Printf(
				TEXT("{\"num_vertices\":%u,\"stride\":%u,\"allocated_size\":%u,\"colors\":["),
				Colors.GetNumVertices(),
				Colors.GetStride(),
				Colors.GetAllocatedSize());
			for (uint32 Index = 0; Index < Colors.GetNumVertices(); ++Index)
			{
				if (Index > 0) ValueJson += TEXT(",");
				const FColor& Color = Colors.VertexColor(Index);
				ValueJson += FString::Printf(
					TEXT("{\"r\":%d,\"g\":%d,\"b\":%d,\"a\":%d}"),
					static_cast<int32>(Color.R),
					static_cast<int32>(Color.G),
					static_cast<int32>(Color.B),
					static_cast<int32>(Color.A));
			}
			ValueJson += TEXT("]}");
		}
	}
	else
	{
		return JsonError(TEXT("Unsupported StaticMeshComponent LOD property: ") + PropertyName);
	}

	return FString::Printf(
		TEXT("{\"status\":\"ok\",\"lod_index\":%d,\"property\":\"%s\",\"value\":%s}"),
		LODIndex,
		*JsonEscape(PropertyName),
		*ValueJson);
}
